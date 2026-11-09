# Local metrics stack

OpenTelemetry Collector, Prometheus, Grafana and Redis for trying the 10xGraph `metrics` block locally. Local use only: no auth, Grafana allows anonymous admin. Ports are bound to 127.0.0.1. URLs use 127.0.0.1 on purpose: where another service listens on ::1, `localhost` can reach the wrong app.

| Service | URL |
|---|---|
| Grafana | http://127.0.0.1:3000 |
| Prometheus | http://127.0.0.1:9090 |
| Collector OTLP | http://127.0.0.1:4318 (HTTP), 127.0.0.1:4317 (gRPC) |
| Redis | 127.0.0.1:6379 |

```bash
docker compose up -d
```

## Point 10xGraph at it

Install `10xgraph-api[metrics]`, then add to `10xgraph.json`.

Option A, push through the collector (recommended; also takes AI traces):

```json
{
  "metrics": { "enabled": true, "export": "otlp", "otlp": { "endpoint": "http://127.0.0.1:4318" } },
  "observability": { "otlp": { "enabled": true, "use_metrics_endpoint": true } }
}
```

Traces land in the collector's debug log (`docker compose logs -f otel-collector`). To keep them, add a Tempo or Jaeger exporter to `otel-collector.yaml`.

Option A2, push straight to Prometheus (metrics only, no AI traces):

```json
{ "metrics": { "enabled": true, "export": "otlp", "otlp": { "endpoint": "http://127.0.0.1:9090/api/v1/otlp" } } }
```

Option B, scrape: set `"export": "prometheus"`. `prometheus.yml` already scrapes the API at `host.docker.internal:8000/metrics` and the worker at `host.docker.internal:9464`. Scrape covers one process; use push with several gunicorn workers.

## Run the app

```bash
OTEL_SERVICE_NAME=tenxgraph-api 10xgraph dev                          # API on :8000
OTEL_SERVICE_NAME=tenxgraph-worker taskiq worker <your.broker:module>  # worker, Redis at redis://127.0.0.1:6379
```

Different `OTEL_SERVICE_NAME` values make `job` separate the API from the worker in Prometheus.

Tip for local testing: metrics are pushed every 60 seconds by default. Set `OTEL_METRIC_EXPORT_INTERVAL=5000` (milliseconds) on both commands to see data in seconds.

If port 6379 is already used by another Redis, change the Redis port mapping in `docker-compose.yml` (for example `127.0.0.1:6380:6379`) and point `cache_store` and the worker URLs at `redis://127.0.0.1:6380`.

Send some requests, then open Grafana at http://127.0.0.1:3000 and the **10xGraph** dashboard. It is provisioned from `grafana/10xgraph-dashboard.json`; the same file can be imported into any Grafana (pick your Prometheus in the Data source variable).
