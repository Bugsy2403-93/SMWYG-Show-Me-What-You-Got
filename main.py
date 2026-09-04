#!/usr/bin/env python3
"""One-shot investigator agent entrypoint for Docker Compose."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from openai import OpenAI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, start_http_server

from agent_controller import AgentController, EventSink


AGENT_RUNS = Counter("agent_runs_total", "Agent run outcomes", ["status"])
AGENT_TOOL_CALLS = Counter("agent_tool_calls_total", "Tool invocations", ["function", "success"])
AGENT_VALIDATION_ERRORS = Counter(
    "agent_validation_errors_total",
    "Rejected model invocations / validation errors",
    ["reason"],
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def setup_tracing() -> None:
    if not _env_bool("OTEL_ENABLED", False):
        return
    endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://localhost:4318/v1/traces",
    )
    service = os.getenv("OTEL_SERVICE_NAME", "investigator-agent")
    resource = Resource.create({"service.name": service})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)


def setup_metrics() -> None:
    port = int(os.getenv("PROMETHEUS_PORT", "9464"))
    start_http_server(port)


def observability_hook(record: dict[str, Any]) -> None:
    event = record.get("event")
    if event == "agent_finished":
        AGENT_RUNS.labels(status=str(record.get("status", "unknown"))).inc()
    elif event == "tool_finished":
        AGENT_TOOL_CALLS.labels(
            function=str(record.get("function", "unknown")),
            success="true",
        ).inc()
    elif event == "tool_failed":
        AGENT_TOOL_CALLS.labels(
            function=str(record.get("function", "unknown")),
            success="false",
        ).inc()
    elif event == "invocation_rejected":
        AGENT_VALIDATION_ERRORS.labels(reason=str(record.get("reason", "unknown"))).inc()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the investigator agent once")
    parser.add_argument(
        "question",
        nargs="*",
        help="Research question / instruction for the agent",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    question = " ".join(args.question).strip() or os.getenv("AGENT_QUESTION", "").strip()
    if not question:
        print("Provide a question as argv or set AGENT_QUESTION", file=sys.stderr)
        return 2

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is required", file=sys.stderr)
        return 2

    setup_metrics()
    setup_tracing()
    tracer = trace.get_tracer("investigator-agent")

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"),
    )
    workspace = os.getenv("AGENT_WORKSPACE", "/data/workspace")
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")
    max_steps = int(os.getenv("MAX_STEPS", "12"))

    controller = AgentController(
        question=question,
        workspace=workspace,
        client=client,
        model=model,
        max_steps=max_steps,
        allow_overwrite=False,
        observability=EventSink(hook=observability_hook),
    )

    with tracer.start_as_current_span("agent.run") as span:
        span.set_attribute("agent.model", model)
        span.set_attribute("agent.max_steps", max_steps)
        result = controller.run()
        span.set_attribute("agent.finished", bool(controller.state.finished))
        print(result.get("answer", result))
        return 0 if controller.state.finished else 1


if __name__ == "__main__":
    raise SystemExit(main())
