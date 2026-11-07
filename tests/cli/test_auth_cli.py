"""``10xgraph audit`` and ``10xgraph build`` on the shared auth rules."""


from __future__ import annotations

import io
import json
from pathlib import Path

from tenxgraph_api.cli.commands.audit import AuditCommand
from tenxgraph_api.cli.commands.build import BuildCommand
from tenxgraph_api.cli.core.output import OutputFormatter


BAD = {"agent": "graph.react:app", "auth": "jwt", "authorization": "ownership"}
GOOD = {"agent": "graph.react:app", "auth": {"method": "jwt"}}


def _write(tmp_path: Path, monkeypatch, config: dict) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "10xgraph.json").write_text(json.dumps(config), encoding="utf-8")


def test_audit_fails_naming_the_rule(tmp_path, monkeypatch) -> None:
    _write(tmp_path, monkeypatch, BAD)
    result = AuditCommand._auth_check()
    assert result.status == "fail"
    assert "authorization-removed" in result.detail


def test_audit_exit_code_is_1_and_output_names_the_rule(tmp_path, monkeypatch) -> None:
    _write(tmp_path, monkeypatch, BAD)
    stream = io.StringIO()
    code = AuditCommand(OutputFormatter(stream=stream)).execute()
    assert code == 1
    assert "authorization-removed" in stream.getvalue()


def test_audit_shows_warnings_without_failing(tmp_path, monkeypatch) -> None:
    _write(tmp_path, monkeypatch, {"agent": "graph.react:app"})
    result = AuditCommand._auth_check()
    assert result.status == "warn"
    assert "auth-off-production" in result.detail


def test_audit_passes_a_good_config(tmp_path, monkeypatch) -> None:
    _write(tmp_path, monkeypatch, GOOD)
    assert AuditCommand._auth_check().status == "pass"


def test_build_warns_that_the_image_would_not_boot(tmp_path, monkeypatch) -> None:
    _write(tmp_path, monkeypatch, BAD)
    stream = io.StringIO()
    code = BuildCommand(OutputFormatter(stream=stream)).execute(force=True)
    text = stream.getvalue()
    assert code == 0
    assert "authorization-removed" in text
    assert "would not boot" in text
