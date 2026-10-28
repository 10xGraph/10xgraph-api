from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import tenxgraph_api.cli.main as main_mod
from tenxgraph_api.cli.exceptions import CLIError


runner = CliRunner()


def test_dev_command_opens_the_playground_by_default(monkeypatch):
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.APICommand, "execute", lambda self, **kwargs: called.update(kwargs) or 0
    )

    result = runner.invoke(main_mod.app, ["dev", "--port", "9001", "--no-reload"])

    assert result.exit_code == 0
    assert called["config"] == "10xgraph.json"
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 9001
    assert called["reload"] is False
    assert called["open_playground"] is True


def test_dev_command_no_open_serves_only(monkeypatch):
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.APICommand, "execute", lambda self, **kwargs: called.update(kwargs) or 0
    )
    result = runner.invoke(
        main_mod.app,
        ["dev", "-c", "custom.json", "-H", "1.2.3.4", "-p", "8080", "--no-reload", "--no-open"],
    )
    assert result.exit_code == 0
    assert called["config"] == "custom.json"
    assert called["host"] == "1.2.3.4"
    assert called["port"] == 8080
    assert called["reload"] is False
    assert called["open_playground"] is False


@pytest.mark.parametrize("removed", ["api", "play"])
def test_api_and_play_commands_are_removed(removed):
    result = runner.invoke(main_mod.app, [removed])
    assert result.exit_code != 0


def test_version_command(monkeypatch):
    called = []
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(main_mod.VersionCommand, "execute", lambda self: called.append(True) or 0)
    result = runner.invoke(main_mod.app, ["version"])
    assert result.exit_code == 0
    assert len(called) == 1


def test_init_command(monkeypatch):
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.InitCommand, "execute", lambda self, **kwargs: called.update(kwargs) or 0
    )
    result = runner.invoke(main_mod.app, ["init", "--path", "test_path", "--force"])
    assert result.exit_code == 0
    assert called["path"] == "test_path"
    assert called["force"] is True


def test_build_command(monkeypatch):
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.BuildCommand, "execute", lambda self, **kwargs: called.update(kwargs) or 0
    )
    result = runner.invoke(
        main_mod.app,
        [
            "build",
            "-o",
            "Dfile",
            "--force",
            "--python-version",
            "3.12",
            "-p",
            "5000",
            "--docker-compose",
        ],
    )
    assert result.exit_code == 0
    assert called["output_file"] == "Dfile"
    assert called["force"] is True
    assert called["python_version"] == "3.12"
    assert called["port"] == 5000
    assert called["docker_compose"] is True


def test_skills_command(monkeypatch):
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.SkillsCommand, "execute", lambda self, **kwargs: called.update(kwargs) or 0
    )
    result = runner.invoke(
        main_mod.app, ["skills", "-a", "codex", "-p", "skills_path", "--force", "--all", "--list"]
    )
    assert result.exit_code == 0
    assert called["agent"] == "codex"
    assert called["path"] == "skills_path"
    assert called["force"] is True
    assert called["all_agents"] is True
    assert called["list_agents"] is True
    assert called["validate_paths"] is None


def test_skills_validate_option(monkeypatch):
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.SkillsCommand, "execute", lambda self, **kwargs: called.update(kwargs) or 0
    )
    result = runner.invoke(main_mod.app, ["skills", "--validate", "a", "--validate", "b"])
    assert result.exit_code == 0
    assert called["validate_paths"] == ["a", "b"]


def test_test_command(monkeypatch):
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.TestCommand, "execute", lambda self, **kwargs: called.update(kwargs) or 0
    )
    result = runner.invoke(
        main_mod.app,
        ["test", "tests/foo.py", "--coverage", "--html", "-k", "foo_test", "--", "--lf", "-vv"],
    )
    assert result.exit_code == 0
    assert called["path"] == "tests/foo.py"
    assert called["coverage"] is True
    assert called["html"] is True
    assert called["keyword"] == "foo_test"
    assert called["extra_args"] == ("--lf", "-vv")


def test_eval_command(monkeypatch):
    called = {}
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.EvalCommand, "execute", lambda self, **kwargs: called.update(kwargs) or 0
    )
    result = runner.invoke(
        main_mod.app,
        [
            "eval",
            "target_eval",
            "-o",
            "out_dir",
            "--no-report",
            "-t",
            "0.8",
            "--parallel",
            "-c",
            "8",
        ],
    )
    assert result.exit_code == 0
    assert called["target"] == "target_eval"
    assert called["output_dir"] == "out_dir"
    assert called["no_report"] is True
    assert called["threshold"] == 0.8
    assert called["parallel"] is True
    assert called["max_concurrency"] == 8


def test_a2a_command_is_not_exposed():
    result = runner.invoke(main_mod.app, ["a2a"])

    assert result.exit_code != 0
    assert "No such command 'a2a'" in result.output


def test_handle_tenxgraph_api_error(monkeypatch):
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.VersionCommand,
        "execute",
        lambda self: (_ for _ in ()).throw(CLIError("Custom error message", exit_code=42)),
    )
    result = runner.invoke(main_mod.app, ["version"])
    assert result.exit_code == 42


def test_handle_generic_exception(monkeypatch):
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        main_mod.VersionCommand,
        "execute",
        lambda self: (_ for _ in ()).throw(ValueError("Some generic value error")),
    )
    result = runner.invoke(main_mod.app, ["version"])
    assert result.exit_code == 1


def test_main_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    with patch("tenxgraph_api.cli.main.app", side_effect=KeyboardInterrupt):
        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        assert exc_info.value.code == 130


def test_main_generic_exception(monkeypatch):
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)
    with patch("tenxgraph_api.cli.main.app", side_effect=ValueError("Main error")):
        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        assert exc_info.value.code == 1


def test_legacy_agentflow_entry_point_warns_then_runs(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(main_mod, "main", lambda: called.append(True))

    main_mod.legacy_main()

    assert called == [True]
    err = capsys.readouterr().err
    assert "'agentflow' command is deprecated" in err
    assert "'10xgraph'" in err


@pytest.mark.parametrize(
    ("env", "flags", "expected"),
    [
        ({}, [], False),  # default: normal scrollback, like gh / uv / cargo
        ({"TENXGRAPH_FULLSCREEN": "1"}, [], True),
        ({"TENXGRAPH_FULLSCREEN": "1", "TENXGRAPH_NO_FULLSCREEN": "1"}, [], False),
        ({}, ["--fullscreen"], True),
        ({"TENXGRAPH_FULLSCREEN": "1"}, ["--no-fullscreen"], False),
    ],
)
def test_fullscreen_is_opt_in(monkeypatch, env, flags, expected):
    for name in ("TENXGRAPH_FULLSCREEN", "TENXGRAPH_NO_FULLSCREEN", "AGENTFLOW_NO_FULLSCREEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    requested: list[bool] = []
    monkeypatch.setattr(main_mod.output, "request_fullscreen", requested.append)
    monkeypatch.setattr(main_mod, "setup_cli_logging", lambda **kwargs: None)

    result = runner.invoke(main_mod.app, [*flags, "--version"])

    assert result.exit_code == 0
    assert requested == [expected]
