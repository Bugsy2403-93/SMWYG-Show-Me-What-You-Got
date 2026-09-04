# OpenTelemetry Collector batch processor notes

Verified 2026-09-04 against the official batch processor README.

- `send_batch_size` defaults to `8192` and acts as a **trigger** after which a batch is sent regardless of timeout; it does **not** enforce maximum batch size.
- `timeout` defaults to `200ms` and sends a batch regardless of size; `timeout: 0` sends immediately subject to `send_batch_max_size`.
- `send_batch_max_size` defaults to `0` (no upper limit). When set, it splits larger batches and must be ≥ `send_batch_size`.
- Place batch **after** `memory_limiter` and sampling processors (batch after drops).
- Metadata batching can increase memory: each metadata combination creates a background task and pending batch.

Source: https://github.com/open-telemetry/opentelemetry-collector/blob/main/processor/batchprocessor/README.md
