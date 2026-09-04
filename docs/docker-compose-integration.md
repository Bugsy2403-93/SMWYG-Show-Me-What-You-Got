# Docker Compose integration testing

The integration workflow starts the observability stack, verifies that Prometheus can scrape the Collector, confirms that alert rules are loaded, runs the one-shot agent, and queries Jaeger for the resulting trace.

## Files

| File | Purpose |
|---|---|
| `docker-compose.integration.yml` | Test override with deterministic agent command and pinned service settings. |
| `integration_test_compose.py` | Starts services, waits for readiness, checks metrics, validates Prometheus rules, runs the agent, and queries Jaeger. |

## Run

Docker must be available:

```bash
python3 integration_test_compose.py
```

The script uses:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.integration.yml \
  up --build -d otel-collector jaeger prometheus
```

It then verifies:

1. Jaeger responds on `http://localhost:16686`.
2. Prometheus is ready on `http://localhost:9090/-/ready`.
3. Collector metrics respond on `http://localhost:8888/metrics`.
4. Collector metrics include accepted-span and queue metrics.
5. Prometheus marks the `otel-collector` target as up.
6. Prometheus loads the five Collector alert rules.
7. The one-shot agent runs and exports a trace through the Collector.
8. Jaeger returns a trace for the `investigator-agent` service.

The script always attempts cleanup with:

```bash
docker compose -f docker-compose.yml -f docker-compose.integration.yml down -v
```

## Alert-rule verification

The test checks that these rules are loaded:

| Alert | Purpose |
|---|---|
| `CollectorQueueNearlyFull` | Queue utilization exceeds 80%. |
| `CollectorEnqueueFailures` | Spans cannot enter the sending queue. |
| `CollectorExportFailures` | Spans fail during export to Jaeger. |
| `CollectorReceiverRefusals` | Receivers refuse incoming spans. |
| `CollectorDown` | Prometheus cannot scrape the Collector. |

Loading a rule proves that Prometheus parsed it. It does not prove that the alert fires. To test firing behavior, stop Jaeger while the Collector and Prometheus remain running:

```bash
docker compose -f docker-compose.yml -f docker-compose.integration.yml stop jaeger
```

Generate traces or wait for exporter retries, then inspect:

```bash
curl http://localhost:9090/api/v1/alerts
```

Depending on timing and queue capacity, `CollectorExportFailures` or `CollectorQueueNearlyFull` should become pending or firing. Restore Jaeger afterward:

```bash
docker compose -f docker-compose.yml -f docker-compose.integration.yml start jaeger
```

## Sandbox limitation

The current sandbox does not provide the Docker executable, so the integration script cannot be executed here. The script and Compose override have been syntax-checked as project files, but runtime verification must be performed on a Docker-enabled host.
