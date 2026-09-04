# Docker deployment

This project runs the agent as a one-shot Compose service and runs Jaeger and Prometheus alongside it. The agent writes approved artifacts to the named `agent_workspace` volume, exports traces to Jaeger through OTLP/HTTP, and exposes Prometheus metrics on port 9464 for scraping.

## Configure secrets

Copy the template and replace the placeholder. Do not commit `.env`:

```bash
cp .env.example .env
$EDITOR .env
```

Set `AGENT_QUESTION` in the shell or add it to `.env` when starting the stack. The Compose command uses a safe default only so configuration validation succeeds; production runs should always provide the real question.

## Build and run

```bash
AGENT_QUESTION="Create an evidence note for the approved research question." \
  docker compose up --build agent jaeger prometheus
```

The agent is configured with `restart: "no"` because it is a one-shot workflow. Jaeger and Prometheus continue running after the agent exits.

The interfaces are:

| Service | Address | Purpose |
|---|---|---|
| Agent metrics | `http://localhost:9464/metrics` | Prometheus scrape endpoint. |
| Jaeger UI | `http://localhost:16686` | Search and inspect agent traces. |
| Prometheus UI | `http://localhost:9090` | Query agent metrics. |
| Jaeger OTLP/HTTP | Internal `http://jaeger:4318/v1/traces` | Trace ingestion endpoint used by the agent. |

To inspect logs:

```bash
docker compose logs -f agent
```

To inspect the shared workspace:

```bash
docker compose run --rm agent \
  "List the files already present in the workspace without changing them."
```

## Prometheus queries

Useful initial queries include:

```promql
agent_runs_total
```

```promql
agent_tool_calls_total
```

```promql
rate(agent_validation_errors_total[5m])
```

## Production cautions

The Compose file is a development and small-server deployment baseline. Store `OPENAI_API_KEY` in a secret manager rather than committing it to `.env`, pin image versions instead of using `latest`, restrict exposed ports with a firewall or reverse proxy, and use a persistent volume backup strategy for `/data/workspace`.

The agent exposes Prometheus on all container interfaces so the Prometheus service can scrape it. Do not publish port 9464 publicly. If Prometheus and the agent are placed behind an ingress, protect the metrics endpoint with network policy or authentication.

The current agent service is one-shot. For a queue worker or API service, replace the CLI entrypoint with a long-running process and define an explicit health endpoint. Do not use an automatic restart policy for a one-shot command unless repeated execution is intentional.

If Jaeger is unavailable, the controller can still complete its local workflow, but traces may be delayed or dropped depending on exporter behavior. Metrics remain available locally through the Prometheus reader. The deterministic pytest suite does not require Docker, Jaeger, Prometheus, or a live model.
