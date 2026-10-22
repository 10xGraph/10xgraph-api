# 10xgraph-api 0.9.0

One server command, a guided `10xgraph init`, leaner production images and Python 3.14 and 3.15 support.

This release has a **breaking change**. Read "Before you upgrade" first.

```bash
pip install -U "10xgraph-api==0.9.0"
```

## Before you upgrade

- **`10xgraph api` and `10xgraph play` are removed. `10xgraph dev` is the only server command.**
  - `10xgraph api` becomes `10xgraph dev --no-open`; `10xgraph play` becomes `10xgraph dev`.
  - The options are unchanged: `--config`, `--host`, `--port`, `--reload/--no-reload`.
  - Production is not affected: images from `10xgraph build` run Gunicorn directly.
- **The WebSocket routes now warn when no WebSocket library is installed.**
  - Before, a project without `google-genai` silently answered 404 on `/v1/graph/ws` and `/v1/graph/live`.
  - Install `10xgraph-api[websocket]`, or set `"websocket": {"enabled": false}` in `10xgraph.json` to leave the routes out.
- **Existing Dockerfiles do not get the smaller image by themselves.** Regenerate them with `10xgraph build --force`.

## New

- **Python 3.14 and 3.15 are officially supported.** CI runs the test suite on 3.12, 3.13, 3.14 and 3.15.
- **`websocket.enabled` in `10xgraph.json`** turns the WebSocket routes on or off, and the new `10xgraph-api[websocket]` extra installs the library they need.
- **`10xgraph init` asks two more questions:**
  - Which AI provider (Google Gemini, OpenAI or Anthropic Claude; `--provider`, default `google`). The model, `.env.example` key, tests and eval simulator follow the answer.
  - Whether to enable WebSocket streaming and realtime audio (`--websocket/--no-websocket`, default off).
- **Production scaffolds list only the dependencies your answers need**, and the generated Dockerfile installs exactly that list.
- **Smaller production images.** The Dockerfile removes pip and the CLI's own libraries (typer, rich, questionary and what only they need), about 38 MB. A library your project still uses is kept.
- **`10xgraph-api[images]` extra** installs Pillow for image resize, EXIF orientation and format conversion, now that Pillow is optional in core `10xgraph`.

## Fixed

- **A production scaffold did not start:**
  - `10xgraph.json` pointed at `graph.agent:container`, which the template never defined.
  - With the Redis rate limiter, `10xgraph.json` had no Redis URL.
  - Its `tests/test_graph_nodes.py` failed on collection.
- **Redis rate limiting from a URL crashed at startup** with `NameError: Redis`.
- **`10xgraph init` generated a `pyproject.toml` claiming Python 3.10.** It now says `>=3.12`, which `10xgraph` needs.

See `CHANGELOG.md` for the full list.
