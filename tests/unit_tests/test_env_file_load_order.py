"""Settings see the ``env`` file from 10xgraph.json under plain gunicorn (L10).

``10xgraph dev`` loads that file before importing the app, but the generated Dockerfile
runs ``gunicorn tenxgraph_api.src.app.main:app`` directly. Settings used to be built (and
cached) before GraphConfig loaded the file, so its values were silently ignored.
"""

# ruff: noqa: S101, S603

import json
import os
import subprocess
import sys


def test_main_reads_settings_after_the_env_file(tmp_path):
    (tmp_path / "app.env").write_text("APP_NAME=from-env-file\n")
    config = tmp_path / "10xgraph.json"
    config.write_text(json.dumps({"agent": "graph.react:app", "env": "app.env"}))

    env = {k: v for k, v in os.environ.items() if k != "APP_NAME"}
    env["GRAPH_PATH"] = str(config)
    code = "import tenxgraph_api.src.app.main as m; print(m.settings.APP_NAME)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "from-env-file"
