"""Root runtime, startup isolation, and new command contract tests."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import tenxgraph_api.cli.main as main_mod
from tenxgraph_api.cli.commands.audit import AuditCommand


runner = CliRunner()


def test_root_help_does_not_import_command_implementations(monkeypatch) -> None:
    imported: list[str] = []
    original = main_mod.importlib.import_module

    def recording_import(name: str, *args, **kwargs):
        if name.startswith("tenxgraph_api.cli.commands."):
            imported.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(main_mod.importlib, "import_module", recording_import)
    result = runner.invoke(main_mod.app, ["--help"])
    assert result.exit_code == 0
    assert imported == []
    assert "dev" in result.output
    assert "audit" in result.output


def test_root_version_is_script_friendly() -> None:
    result = runner.invoke(main_mod.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == main_mod.CLI_VERSION


def test_plain_and_no_color_global_options() -> None:
    result = runner.invoke(main_mod.app, ["--format", "plain", "--color", "never", "version"])
    assert result.exit_code == 0
    assert "\x1b[" not in result.output
    assert "Version" in result.output


def test_no_animation_alias_selects_static_output() -> None:
    result = runner.invoke(main_mod.app, ["--no-animation", "audit"])
    assert result.exit_code == 0
    assert "Audit" in result.output


def test_audit_validates_remote_tool_schema(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "10xgraph.json").write_text(
        json.dumps(
            {
                "agent": "graph.agent:app",
                "remote_tools": [
                    {
                        "node": "tools",
                        "name": "read_clipboard",
                        "description": "Read clipboard text.",
                        "parameters": {"type": "object"},
                    }
                ],
            }
        )
    )

    diagnostic = AuditCommand._config_check()
    assert diagnostic.status == "pass"
    assert "1 remote tools" in diagnostic.detail


def test_audit_fails_for_misspelled_remote_tool_key(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "10xgraph.json").write_text(
        json.dumps(
            {
                "agent": "graph.agent:app",
                "remote_tools": [
                    {"nod": "tools", "name": "read_clipboard", "description": "Read clipboard."}
                ],
            }
        )
    )

    result = runner.invoke(main_mod.app, ["--no-animation", "audit"])
    assert result.exit_code == 1
    assert "remote_tools" in result.output
    assert "nod" in result.output


def test_audit_accepts_explicit_project_config(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "custom.json"
    config_path.write_text(json.dumps({"agent": "graph.agent:app", "remote_tools": []}))

    diagnostic = AuditCommand._config_check(str(config_path))
    assert diagnostic.status == "pass"

    missing = runner.invoke(main_mod.app, ["--no-animation", "audit", "--config", "missing.json"])
    assert missing.exit_code == 1
    assert "not found" in missing.output


def _audit_with_namer(monkeypatch, tmp_path, value):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "10xgraph.json").write_text(
        json.dumps({"agent": "graph.agent:app", "thread_name_generator": value})
    )
    return AuditCommand._config_check()


@pytest.mark.parametrize("value", [None, "", "none", "random", "first_message"])
def test_audit_accepts_builtin_thread_name_generators(monkeypatch, tmp_path, value) -> None:
    assert _audit_with_namer(monkeypatch, tmp_path, value).status == "pass"


def test_audit_accepts_custom_thread_name_generator_path(monkeypatch, tmp_path) -> None:
    (tmp_path / "namer.py").write_text("")
    assert _audit_with_namer(monkeypatch, tmp_path, "namer:MyNamer").status == "pass"


@pytest.mark.parametrize("value", ["random_name", "firstmessage", "foo:", 3])
def test_audit_fails_for_bad_thread_name_generator(monkeypatch, tmp_path, value) -> None:
    diagnostic = _audit_with_namer(monkeypatch, tmp_path, value)
    assert diagnostic.status == "fail"
    assert "none, random, first_message or module:attribute" in diagnostic.detail


def test_audit_warns_when_thread_name_module_is_missing(monkeypatch, tmp_path) -> None:
    diagnostic = _audit_with_namer(monkeypatch, tmp_path, "missing_mod:Namer")
    assert diagnostic.status == "warn"
    assert "missing_mod" in diagnostic.detail


def test_dev_delegates_to_api_with_open_policy(monkeypatch) -> None:
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.APICommand,
        "execute",
        lambda self, **kwargs: called.update(kwargs) or 0,
    )
    result = runner.invoke(main_mod.app, ["dev", "--no-open", "--port", "8123"])
    assert result.exit_code == 0
    assert called["open_playground"] is False
    assert called["port"] == 8123


def test_lazy_dependency_error_has_recovery_code(monkeypatch) -> None:
    original = main_mod.importlib.import_module

    def fail_eval(name: str, *args, **kwargs):
        if name == "tenxgraph_api.cli.commands.eval":
            raise ImportError("missing evaluation symbol")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(main_mod.importlib, "import_module", fail_eval)
    result = runner.invoke(main_mod.app, ["eval"])
    assert result.exit_code == 4
    assert "AF-DEPS-001" in result.output
    assert "10xgraph audit" in result.output
