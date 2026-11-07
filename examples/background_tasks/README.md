# Background tasks example

A tiny graph (no model call) and two tasks: `make_report` (started over HTTP) and `heartbeat` (sent every minute by the scheduler). Full guide: the "Background tasks" page in the server docs.

Auth is off here to keep the curl calls short. In production turn on `auth` and `authorization`; tasks are then owner-only.

## Run

```bash
pip install "10xgraph-api[jobs]"
export REDIS_URL=redis://localhost:6379/0   # any Redis you can use

10xgraph dev --no-open      # terminal 1: the API (starts tasks, does not run them)
10xgraph worker             # terminal 2: runs tasks
10xgraph scheduler          # terminal 3: sends heartbeat; run exactly one
```

The task name is the `task_name` set on the decorator, here `make_report`.

## Try it

```bash
# start (returns {"data": {"task_id": "..."}})
curl -X POST http://127.0.0.1:8000/v1/tasks/make_report \
  -H 'content-type: application/json' -d '{"kwargs": {"topic": "sales"}}'

# status, progress and result
curl http://127.0.0.1:8000/v1/tasks/<task_id>

# cancel (make_report checks ctx.cancelled() each step)
curl -X POST http://127.0.0.1:8000/v1/tasks/<task_id>/cancel
```

`heartbeat` is not `http=True`, so the API answers 404 for it; the scheduler runs it from its `schedule=` label and the worker prints `heartbeat` every minute.
