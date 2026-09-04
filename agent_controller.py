from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class ValidationError(ValueError):
    pass


MAX_WRITE_BYTES = 1_048_576
MAX_PATH_LENGTH = 512


@dataclass
class AgentState:
    question: str
    workspace: Path
    max_steps: int = 8
    step: int = 0
    observations: List[Dict[str, Any]] = field(default_factory=list)
    finished: bool = False

    def context(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "workspace": str(self.workspace),
            "step": self.step,
            "max_steps": self.max_steps,
            "observations": self.observations[-10:],
        }


class SafeFileWriter:
    def __init__(
        self,
        workspace: str | Path,
        *,
        allow_overwrite: bool = False,
        allowed_extensions: Optional[set[str]] = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.allow_overwrite = allow_overwrite
        self.allowed_extensions = allowed_extensions

    def _safe_destination(self, filename: str) -> Path:
        if "\x00" in filename:
            raise PermissionError("NUL bytes are forbidden")
        if len(filename) > MAX_PATH_LENGTH:
            raise PermissionError("path exceeds maximum length")

        raw = Path(filename).expanduser()
        candidate = raw if raw.is_absolute() else self.workspace / raw
        candidate = candidate.resolve(strict=False)

        try:
            relative = candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionError(
                f"path escapes workspace: {candidate}"
            ) from exc

        if not relative.parts:
            raise PermissionError("cannot write to workspace directory")

        current = self.workspace
        for part in relative.parts[:-1]:
            current /= part
            if current.exists() and current.is_symlink():
                raise PermissionError(f"symlinked parent is not allowed: {current}")

        if candidate.exists() and candidate.is_symlink():
            raise PermissionError("destination is a symlink")
        if candidate.exists() and not candidate.is_file():
            raise PermissionError("destination is not a regular file")

        if self.allowed_extensions is not None:
            if candidate.suffix.lower() not in self.allowed_extensions:
                raise PermissionError("file extension is not allowed")

        return candidate

    def write(self, file: str, content: str, mode: str) -> Dict[str, Any]:
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ValueError("content exceeds the 1 MiB limit")
        if mode not in {"fail_if_exists", "replace", "append"}:
            raise ValidationError("unsupported write mode")

        target = self._safe_destination(file)
        existed_before = target.exists()

        if mode == "fail_if_exists" and existed_before:
            raise FileExistsError(f"file already exists: {target}")
        if mode == "replace" and not self.allow_overwrite:
            raise PermissionError("replace mode is disabled by controller policy")

        if mode == "append":
            if not existed_before:
                raise FileNotFoundError("append mode requires an existing file")
            with target.open("a", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return {
                "file": str(target),
                "mode": mode,
                "bytes_written": len(content.encode("utf-8")),
                "overwrote": False,
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())

            if mode == "fail_if_exists" and target.exists():
                raise FileExistsError(f"file appeared before creation: {target}")
            if target.exists() and target.is_symlink():
                raise PermissionError("destination became a symlink")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        return {
            "file": str(target),
            "mode": mode,
            "bytes_written": len(content.encode("utf-8")),
            "overwrote": existed_before,
        }


class EventSink:
    """Small observability abstraction; works with logging, OTel, or tests."""

    def __init__(self, hook: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.hook = hook
        self.events: List[Dict[str, Any]] = []

    def emit(self, event: str, **fields: Any) -> None:
        record = {"event": event, **fields}
        self.events.append(record)
        if self.hook:
            try:
                self.hook(record)
            except Exception:
                pass


class AgentController:
    def __init__(
        self,
        question: str,
        workspace: str,
        *,
        client: Any,
        model: str = "gpt-5-mini",
        max_steps: int = 8,
        allow_overwrite: bool = False,
        observability: Optional[EventSink] = None,
    ) -> None:
        self.state = AgentState(
            question=question,
            workspace=Path(workspace).resolve(),
            max_steps=max_steps,
        )
        self.writer = SafeFileWriter(
            self.state.workspace,
            allow_overwrite=allow_overwrite,
        )
        self.client = client
        self.model = model
        self.obs = observability or EventSink()
        self.messages: List[Dict[str, str]] = [
            {"role": "system", "content": "Use file_write_safe or final_answer."},
            {"role": "user", "content": question},
        ]

    def run(self) -> Dict[str, Any]:
        started = time.perf_counter()
        self.obs.emit("agent_started", model=self.model, max_steps=self.state.max_steps)

        while self.state.step < self.state.max_steps:
            self.state.step += 1
            self.obs.emit("step_started", step=self.state.step)
            self.messages.append({
                "role": "user",
                "content": "CURRENT_STATE_JSON\n" + json.dumps(self.state.context()),
            })

            model_started = time.perf_counter()
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.messages,
                    response_format={"type": "json_object"},
                    max_completion_tokens=2000,
                )
                raw = response.choices[0].message.content or ""
                self.obs.emit(
                    "model_call_finished",
                    step=self.state.step,
                    duration_ms=round((time.perf_counter() - model_started) * 1000, 2),
                    response_chars=len(raw),
                )
            except Exception as exc:
                self.obs.emit("model_call_failed", step=self.state.step, error_type=type(exc).__name__)
                raise

            try:
                action = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._feedback(f"invalid JSON: {exc}")
                self.obs.emit("invocation_rejected", step=self.state.step, reason="invalid_json")
                continue

            if action.get("type") == "final_answer":
                if not isinstance(action.get("answer"), str) or not action["answer"].strip():
                    self._feedback("final_answer.answer must be non-empty")
                    self.obs.emit("invocation_rejected", step=self.state.step, reason="empty_answer")
                    continue
                self.state.finished = True
                self.obs.emit(
                    "agent_finished",
                    status="success",
                    steps=self.state.step,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                return action

            try:
                invocation = self._validate_invocation(action)
                self.obs.emit(
                    "invocation_validated",
                    step=self.state.step,
                    function=invocation["function"],
                    risk_level=invocation["risk_level"],
                )
            except ValidationError as exc:
                self._feedback(f"invocation rejected: {exc}")
                self.obs.emit(
                    "invocation_rejected",
                    step=self.state.step,
                    reason="schema_or_policy",
                )
                continue

            function = invocation["function"]
            tool_started = time.perf_counter()
            self.obs.emit("tool_started", step=self.state.step, function=function)
            try:
                result = self._dispatch(invocation)
                observation = {"ok": True, "result": result}
                self.obs.emit(
                    "tool_finished",
                    step=self.state.step,
                    function=function,
                    success=True,
                    duration_ms=round((time.perf_counter() - tool_started) * 1000, 2),
                )
            except Exception as exc:
                observation = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                self.obs.emit(
                    "tool_failed",
                    step=self.state.step,
                    function=function,
                    success=False,
                    error_type=type(exc).__name__,
                    duration_ms=round((time.perf_counter() - tool_started) * 1000, 2),
                )

            self.state.observations.append({
                "step": self.state.step,
                "function": function,
                "observation": observation,
            })
            self.messages.extend([
                {"role": "assistant", "content": json.dumps(invocation)},
                {"role": "user", "content": "FUNCTION_RESULT_JSON\n" + json.dumps(observation)},
            ])

        self.obs.emit(
            "agent_finished",
            status="step_limit",
            steps=self.state.step,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return {
            "type": "final_answer",
            "answer": "The controller stopped at its maximum step count.",
            "uncertainties": ["No validated final answer was produced."],
        }

    @staticmethod
    def _validate_invocation(action: Any) -> Dict[str, Any]:
        if not isinstance(action, dict):
            raise ValidationError("action must be an object")
        required = {"function", "arguments", "purpose", "expected_result", "risk_level"}
        if set(action) != required:
            raise ValidationError("invocation has incorrect fields")
        if action["function"] != "file_write_safe":
            raise ValidationError("function is not allow-listed")
        if action["risk_level"] != "local_write":
            raise ValidationError("risk level must be local_write")
        arguments = action["arguments"]
        if not isinstance(arguments, dict) or set(arguments) != {"file", "content", "mode"}:
            raise ValidationError("file_write_safe requires file, content, and mode")
        if not isinstance(arguments["file"], str) or not arguments["file"].strip():
            raise ValidationError("file must be a non-empty string")
        if not isinstance(arguments["content"], str):
            raise ValidationError("content must be a string")
        if arguments["mode"] not in {"fail_if_exists", "replace", "append"}:
            raise ValidationError("invalid write mode")
        return action

    def _dispatch(self, invocation: Dict[str, Any]) -> Dict[str, Any]:
        return self.writer.write(**invocation["arguments"])

    def _feedback(self, message: str) -> None:
        self.messages.append({
            "role": "user",
            "content": "VALIDATION_ERROR\n" + message,
        })
