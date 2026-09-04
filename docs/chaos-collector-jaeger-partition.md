# Chaos testing: Collector → Jaeger network partition

**Goal:** Confirm the OpenTelemetry Collector stays healthy when OTLP export to Jaeger is partitioned: it keeps accepting spans, queues (and/or fails) exports visibly, alerts fire, and recovery drains the queue without leaking span payloads into logs or alerts.

**Paths:** Toxiproxy for Docker Compose; Chaos Mesh for Kubernetes. Same success criteria for both.

## Assumptions

- Chaos path only breaks **export** to Jaeger. Receivers and Prometheus scrape stay up.
- Compose chaos override sends the Collector’s OTLP/HTTP (or gRPC) exporter to Toxiproxy, not straight to Jaeger.
- Prometheus scrapes Collector internal metrics (default `:8888`) and loads alert rules for queue pressure / send failures.
- Batch processor sits **after** `memory_limiter` and any sampling. Defaults under backlog: `send_batch_size` is a *send trigger* (default `8192`), not a hard max; `timeout` defaults to `200ms`; `send_batch_max_size` defaults to `0` (unlimited) unless set ≥ `send_batch_size`.
- Never log, alert on, or screenshot raw span attributes or bodies. Metrics and queue depth only.

## Metrics that matter

Prefer these Prometheus names (Collector internal telemetry). Label by `exporter` if more than one is configured.

| Metric | Role in this test |
| --- | --- |
| `otelcol_receiver_accepted_spans` | Inbound still working during partition |
| `otelcol_receiver_refused_spans` | Should stay flat unless you also overload memory_limiter |
| `otelcol_exporter_queue_size` / `otelcol_exporter_queue_capacity` | Backpressure building toward Jaeger |
| `otelcol_exporter_send_failed_spans` | Export attempts failing (retries may still succeed later — not automatic data loss) |
| `otelcol_exporter_enqueue_failed_spans` | Queue full → drops before retry; appears if partition is long enough |
| `otelcol_exporter_sent_spans` | Should stall or slow during partition; resume on recovery |

Quick scrape helper:

```bash
curl -sS http://localhost:8888/metrics | grep -E \
  'otelcol_receiver_(accepted|refused)_spans|otelcol_exporter_(queue_size|queue_capacity|send_failed_spans|enqueue_failed_spans|sent_spans)'
```

## Shared success criteria

| Phase | Expect |
| --- | --- |
| Baseline | Collector up; `queue_size` near 0; send/enqueue failures not climbing; `sent_spans` and Jaeger UI show fresh traces |
| Partition | `accepted_spans` still climbing; `send_failed_spans` up; `queue_size` rising vs `queue_capacity`; alerts firing; `refused_spans` not the primary failure mode |
| Recovery | Chaos cleared; `queue_size` drains; `sent_spans` resumes; new traces in Jaeger; no sensitive span content in logs/alerts |

**Abort if:** Collector OOMs, inbound clients start failing while export is the only intended fault, or logs dump full span payloads.

Optional late-partition check: if the queue saturates, `enqueue_failed_spans` may rise — that is data loss at the queue edge, not a substitute for watching `send_failed_spans` early in the window.

---

## Path A — Toxiproxy (Docker Compose)

### Wire-up (required)

In `docker-compose.chaos.yml`, the Collector exporter endpoint must target the proxy listen address, for example:

```yaml
# docker-compose.chaos.yml (illustrative)
services:
  otel-collector:
    environment:
      # Match your config templating; HTTP OTLP to Jaeger via Toxiproxy
      JAEGER_OTLP_ENDPOINT: http://toxiproxy:14318
  toxiproxy:
    image: ghcr.io/shopify/toxiproxy:2.9.0
    ports:
      - "8474:8474"
      - "14318:14318"
```

If the Collector still points at `jaeger:4318`, disabling the proxy will do nothing useful.

### 1. Start the chaos stack

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.chaos.yml \
  up --build -d jaeger toxiproxy otel-collector prometheus
```

Wait until healthy: `docker compose ps`.

### 2. Create (or enable) the Jaeger OTLP proxy

```bash
curl -sS -X POST http://localhost:8474/proxies \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "jaeger_otlp",
    "listen": "0.0.0.0:14318",
    "upstream": "jaeger:4318",
    "enabled": true
  }'
```

If it already exists (`409` / already created), enable instead:

```bash
curl -sS -X POST http://localhost:8474/proxies/jaeger_otlp \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true}'
```

Confirm: `curl -sS http://localhost:8474/proxies | jq .`

Use gRPC (`4317`) instead of HTTP (`4318`) only if your Collector exporter is gRPC — keep listen/upstream/protocol consistent.

### 3. Baseline

Generate a short burst, then snapshot metrics and alerts.

**Load (pick one):**

```bash
# A) Project script if you have one
# ./scripts/generate-otlp-load.sh --duration 30s --rps 50

# B) telemetrygen (traces → Collector OTLP gRPC on 4317; adjust host/port)
telemetrygen traces --otlp-endpoint localhost:4317 --otlp-insecure \
  --duration 30s --rate 50
```

```bash
curl -sS http://localhost:8888/metrics | grep -E \
  'otelcol_receiver_(accepted|refused)_spans|otelcol_exporter_(queue_size|queue_capacity|send_failed_spans|enqueue_failed_spans|sent_spans)'
curl -sS 'http://localhost:9090/api/v1/alerts' | jq '.data.alerts[]? | {alertname: .labels.alertname, state}'
```

Confirm fresh traces in Jaeger before partitioning.

### 4. Partition

```bash
curl -sS -X POST http://localhost:8474/proxies/jaeger_otlp \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false}'
```

Keep load running **1–2+ minutes**. Batch `timeout` is ~200ms; queue depth and alert evaluation need sustained export failure, not a blip.

### 5. Verify during partition

```bash
curl -sS http://localhost:8888/metrics | grep -E \
  'otelcol_receiver_(accepted|refused)_spans|otelcol_exporter_(queue_size|queue_capacity|send_failed_spans|enqueue_failed_spans|sent_spans)'
curl -sS 'http://localhost:9090/api/v1/alerts' | jq '.data.alerts[]? | select(.state=="firing")'
```

Pass checks:

- [ ] `accepted_spans` still increasing (load client succeeding)
- [ ] `send_failed_spans` up vs baseline
- [ ] `queue_size` up (and ideally a meaningful fraction of `queue_capacity`)
- [ ] At least one expected alert `firing`
- [ ] `refused_spans` not explaining the symptom

### 6. Restore and verify recovery

```bash
curl -sS -X POST http://localhost:8474/proxies/jaeger_otlp \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true}'
```

Continue light load until `queue_size` drains, `sent_spans` moves again, and Jaeger shows new traces.

### 7. Tear down (optional)

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.chaos.yml \
  down
```

---

## Path B — Chaos Mesh (Kubernetes)

Namespace below: `observability`. Adjust labels to match your chart (`investigator-agent` / Jaeger).

### 1. Preflight

```bash
kubectl -n observability get pods
kubectl -n observability port-forward svc/otel-collector 8888:8888 >/tmp/pf-otelcol.log 2>&1 &
# Port-forward Prometheus / Jaeger the same way your chart exposes them
```

Run the same baseline load + metric snapshot as Path A. Confirm traces reach Jaeger.

### 2. Apply a *bounded* network partition

Sample manifest: [`chaos/jaeger-network-partition.yaml`](chaos/jaeger-network-partition.yaml) (also in this docs folder).

```bash
kubectl apply -f chaos/jaeger-network-partition.yaml
kubectl -n observability describe networkchaos collector-to-jaeger-partition
```

Rules for the manifest:

- Target **only** Collector → Jaeger (selectors + `direction: to`)
- Set an explicit `duration` (sample uses `5m`) so the experiment cannot run forever
- Do not partition the whole namespace or the Prometheus scrape path

### 3. Load + verify during chaos

```bash
kubectl -n observability logs deploy/otel-collector --tail=100
curl -sS http://localhost:8888/metrics | grep -E \
  'otelcol_receiver_(accepted|refused)_spans|otelcol_exporter_(queue_size|queue_capacity|send_failed_spans|enqueue_failed_spans|sent_spans)'
```

Same partition checklist as Path A. Do not dump span payloads from logs.

### 4. End chaos and confirm recovery

If the object has not already expired:

```bash
kubectl -n observability delete networkchaos collector-to-jaeger-partition
```

Confirm it is gone, queue drains, alerts settle, new traces in Jaeger. Kill leftover port-forwards when done.

---

## Suggested alert sketches (Prometheus)

Tune `for` to your scrape interval; keep labels free of span content.

```yaml
groups:
  - name: otelcol-exporter
    rules:
      - alert: OtelColExporterQueueHigh
        expr: |
          otelcol_exporter_queue_size
          / clamp_min(otelcol_exporter_queue_capacity, 1) > 0.7
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: Collector exporter queue >70% capacity
      - alert: OtelColExporterSendFailing
        expr: increase(otelcol_exporter_send_failed_spans[5m]) > 0
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: Collector exporter send failures increasing
```

---

## Batch / memory notes (why the queue behaves this way)

- Unlimited `send_batch_max_size` (`0`) plus a large `sending_queue.queue_size` can grow memory fast under partition. For chaos configs, set `send_batch_max_size` ≥ `send_batch_size` and keep an explicit queue size.
- Metadata-based batching multiplies pending batches (one task per metadata combination) — leave it off unless that is the stress target.
- `memory_limiter` before `batch` so pressure sheds *before* batches pile up.
- Default `sending_queue`: enabled, `queue_size` 1000 (in queue sizer units), `num_consumers` 10. `otelcol_exporter_enqueue_failed_*` is what you see when the queue rejects new data.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| No failure metrics during “partition” | Exporter still talks direct to Jaeger; proxy/chaos not on the export path |
| Inbound clients fail immediately | Fault hit the receiver (wrong Chaos Mesh direction/selectors) |
| Queue never grows | `sending_queue.enabled: false`, load too low/brief, or wrong metric scrape target |
| `send_failed` up but no data loss concern yet | Retries still in play; watch `enqueue_failed_*` and post-recovery Jaeger gaps |
| OOM during test | Unlimited batch max + large queue; tighten `memory_limiter` and `send_batch_max_size` |
| Alerts never fire | Rules missing, wrong metric names, or `for:` longer than the test window |
