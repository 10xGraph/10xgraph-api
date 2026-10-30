import os

import pytest

from tenxgraph_api.cli.commands.api import APICommand
from tenxgraph_api.cli.commands.build import BuildCommand
from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.core.output import OutputFormatter


TEST_PORT = 1234


class SilentOutput(OutputFormatter):  # minimize noise
    def print_banner(self, *args, **kwargs):  # type: ignore[override]
        pass

    def success(self, *args, **kwargs):  # type: ignore[override]
        pass

    def info(self, *args, **kwargs):  # type: ignore[override]
        pass

    def warning(self, *args, **kwargs):  # type: ignore[override]
        pass

    def error(self, *args, **kwargs):  # type: ignore[override]
        pass


@pytest.fixture()
def silent_output():
    return SilentOutput()


def test_api_command_minimal_success(monkeypatch, tmp_path, silent_output):
    monkeypatch.setenv("GRAPH_PATH", "")

    def fake_validate(host, port, config):
        return {"host": host, "port": port, "config": config}

    class FakeConfigManager:
        def find_config_file(self, cfg):
            p = tmp_path / cfg
            p.write_text("{}", encoding="utf-8")
            return p

        def load_config(self, path):  # - simple stub
            return {}

        def resolve_env_file(self):
            return None

    monkeypatch.setitem(os.environ, "PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr("tenxgraph_api.cli.commands.api.validate_cli_options", fake_validate)
    monkeypatch.setattr("tenxgraph_api.cli.commands.api.ConfigManager", lambda: FakeConfigManager())

    called = {}

    def fake_run(app_path, host, port, reload, workers, ws_max_size=None):
        called.update(
            {
                "app_path": app_path,
                "host": host,
                "port": port,
                "reload": reload,
                "workers": workers,
                "ws_max_size": ws_max_size,
            }
        )

    monkeypatch.setattr("tenxgraph_api.cli.commands.api.uvicorn.run", fake_run)
    monkeypatch.delenv("MAX_REQUEST_SIZE", raising=False)

    cmd = APICommand(output=silent_output)
    code = cmd.execute(config="test_config.json", host="127.0.0.1", port=TEST_PORT, reload=False)
    assert code == 0
    assert called["app_path"].endswith(":app")
    # WebSocket frames are capped like HTTP bodies.
    assert called["ws_max_size"] == 10 * 1024 * 1024
    assert called["port"] == TEST_PORT
    assert os.environ.get("GRAPH_PATH", "").endswith("test_config.json")


def test_api_command_error_path(monkeypatch, silent_output):
    def bad_validate(host, port, config):
        raise ValueError("bad input")

    monkeypatch.setattr("tenxgraph_api.cli.commands.api.validate_cli_options", bad_validate)
    cmd = APICommand(output=silent_output)
    code = cmd.execute(config="missing.json")
    assert code == 1


def test_api_command_builds_local_playground_url_for_wildcard_host(silent_output):
    cmd = APICommand(output=silent_output)
    url = cmd._build_playground_url(
        cmd._normalize_browser_host("0.0.0.0"),
        8000,
        "https://play.10xgraph.com",
    )

    assert url == "https://play.10xgraph.com?backendUrl=http%3A%2F%2F127.0.0.1%3A8000"


def test_api_command_schedules_playground_launch(monkeypatch, tmp_path, silent_output):
    def fake_validate(host, port, config):
        return {"host": host, "port": port, "config": config}

    class FakeConfigManager:
        def find_config_file(self, cfg):
            p = tmp_path / cfg
            p.write_text("{}", encoding="utf-8")
            return p

        def load_config(self, path):  # - simple stub
            return {}

        def resolve_env_file(self):
            return None

    scheduled = {}

    def fake_schedule(self, host, port, playground_base_url):
        scheduled.update(
            {
                "host": host,
                "port": port,
                "playground_base_url": playground_base_url,
            }
        )

    monkeypatch.setattr("tenxgraph_api.cli.commands.api.validate_cli_options", fake_validate)
    monkeypatch.setattr("tenxgraph_api.cli.commands.api.ConfigManager", lambda: FakeConfigManager())
    monkeypatch.setattr("tenxgraph_api.cli.commands.api.uvicorn.run", lambda *args, **kwargs: None)
    monkeypatch.setattr(APICommand, "_schedule_playground_launch", fake_schedule)

    cmd = APICommand(output=silent_output)
    code = cmd.execute(
        config="test_config.json",
        host="127.0.0.1",
        port=TEST_PORT,
        reload=False,
        open_playground=True,
    )

    assert code == 0
    assert scheduled == {
        "host": "127.0.0.1",
        "port": TEST_PORT,
        "playground_base_url": "https://play.10xgraph.com",
    }


def _skip_binary(original):
    """Wrap _should_skip to also exclude non-text template artifacts."""

    def patched(self, src, template_dir, context, is_prod):
        if any(part in {".ruff_cache", "__pycache__"} for part in src.parts):
            return True
        return original(self, src, template_dir, context, is_prod)

    return patched


def test_init_command_basic(monkeypatch, tmp_path, silent_output):
    ctx = {
        "agent_name": "MyAgent",
        "agent_name_slug": "my-agent",
        "setup_type": "quick_start",
        "auth": "none",
        "rate_limit": "none",
    }
    monkeypatch.setattr(InitCommand, "_prompt_user", lambda self: ctx)
    cmd = InitCommand(output=silent_output)
    code = cmd.execute(path=str(tmp_path), force=False)
    assert code == 0
    assert (tmp_path / "10xgraph.json").exists()
    assert (tmp_path / "graph" / "agent.py").exists()
    assert (tmp_path / "graph" / "__init__.py").exists()


def test_init_command_prod(monkeypatch, tmp_path, silent_output):
    ctx = {
        "agent_name": "MyAgent",
        "agent_name_slug": "my-agent",
        "setup_type": "production",
        "auth": "none",
        "rate_limit": "none",
    }
    monkeypatch.setattr(InitCommand, "_prompt_user", lambda self: ctx)
    monkeypatch.setattr(InitCommand, "_should_skip", _skip_binary(InitCommand._should_skip))
    cmd = InitCommand(output=silent_output)
    code = cmd.execute(path=str(tmp_path), force=False)
    assert code == 0
    assert (tmp_path / "10xgraph.json").exists()
    assert (tmp_path / "pyproject.toml").exists()
    assert any(
        (tmp_path / f).exists() for f in (".pre-commit-config.yaml", ".pre-commot-config.yaml")
    )


def test_init_command_existing_without_force(monkeypatch, tmp_path, silent_output):
    ctx = {
        "agent_name": "MyAgent",
        "agent_name_slug": "my-agent",
        "setup_type": "quick_start",
        "auth": "none",
        "rate_limit": "none",
    }
    monkeypatch.setattr(InitCommand, "_prompt_user", lambda self: ctx)
    cfg = tmp_path / "10xgraph.json"
    cfg.write_text("{}", encoding="utf-8")
    cmd = InitCommand(output=silent_output)
    code = cmd.execute(path=str(tmp_path), force=False)
    assert code == 1


def test_init_without_a_terminal_explains_the_flag_alternative(tmp_path, silent_output):
    """No TTY and no --yes/--non-interactive is a usage problem with a fix."""
    cmd = InitCommand(output=silent_output)
    code = cmd.execute(path=str(tmp_path), force=False)

    assert code == 3
    assert list(tmp_path.iterdir()) == []


def test_build_command_basic_no_requirements(tmp_path, monkeypatch, silent_output):
    monkeypatch.chdir(tmp_path)
    cmd = BuildCommand(output=silent_output)
    code = cmd.execute(output_file="Dockerfile", force=True, docker_compose=False)
    assert code == 0
    content = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM" in content
    assert "CMD" in content
    dockerignore = (tmp_path / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore


def test_build_command_with_compose(tmp_path, monkeypatch, silent_output):
    monkeypatch.chdir(tmp_path)
    cmd = BuildCommand(output=silent_output)
    code = cmd.execute(
        output_file="Dockerfile",
        force=True,
        docker_compose=True,
        service_name="svc",
    )
    assert code == 0
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM" in dockerfile
    # The dockerfile should include the HEALTHCHECK CMD line but omit the final
    # application run command (CMD ["gunicorn", ...]) when docker_compose=True (omit_cmd=True).
    # 'gunicorn' will still appear in the installation RUN line, so we specifically
    # assert that no line starts with the application CMD instruction.
    assert 'CMD ["gunicorn"' not in dockerfile
    assert (tmp_path / "docker-compose.yml").exists()


def test_build_command_compose_existing_without_force(tmp_path, monkeypatch, silent_output):
    monkeypatch.chdir(tmp_path)
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("version: '3'", encoding="utf-8")
    cmd = BuildCommand(output=silent_output)
    code = cmd.execute(output_file="Dockerfile", force=False, docker_compose=True)
    assert code == 1


def test_init_command_force_overwrite(monkeypatch, tmp_path, silent_output):
    # Create initial files
    cfg = tmp_path / "10xgraph.json"
    agent_dir = tmp_path / "graph"
    agent_dir.mkdir()
    agent_file = agent_dir / "agent.py"
    cfg.write_text("{}", encoding="utf-8")
    agent_file.write_text("print('old')", encoding="utf-8")
    # Execute with force=True should succeed (0) and overwrite
    ctx = {
        "agent_name": "MyAgent",
        "agent_name_slug": "my-agent",
        "setup_type": "quick_start",
        "auth": "none",
        "rate_limit": "none",
    }
    monkeypatch.setattr(InitCommand, "_prompt_user", lambda self: ctx)
    cmd = InitCommand(output=silent_output)
    code = cmd.execute(path=str(tmp_path), force=True)
    assert code == 0
    # Confirm file content overwritten (no longer the initial minimal JSON '{}')
    new_content = cfg.read_text(encoding="utf-8")
    assert new_content.strip() != "{}"
    assert '"agent"' in new_content


def test_build_command_multiple_requirements(tmp_path, monkeypatch, silent_output):
    monkeypatch.chdir(tmp_path)
    # Create multiple requirement files so branch logging about multiple found triggers
    (tmp_path / "requirements.txt").write_text("fastapi==0.1", encoding="utf-8")
    req_dir = tmp_path / "requirements"
    req_dir.mkdir()
    (req_dir / "base.txt").write_text("uvicorn==0.1", encoding="utf-8")
    cmd = BuildCommand(output=silent_output)
    code = cmd.execute(output_file="Dockerfile", force=True, docker_compose=False)
    assert code == 0
    content = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    # Should still include CMD (not docker-compose) and chosen first requirements.txt
    assert 'CMD ["gunicorn"' in content
    assert "requirements.txt" in content


def test_build_command_compose_force_overwrite(tmp_path, monkeypatch, silent_output):
    monkeypatch.chdir(tmp_path)
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  old: {}\n", encoding="utf-8")
    cmd = BuildCommand(output=silent_output)
    code = cmd.execute(output_file="Dockerfile", force=True, docker_compose=True)
    assert code == 0
    content = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    assert "\nservices:\n" in content
    assert "old: {}" not in content


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 10 * 1024 * 1024), ("2048", 2048), ("junk", 10 * 1024 * 1024), ("0", 10 * 1024 * 1024)],
)
def test_ws_max_size_follows_max_request_size(monkeypatch, raw, expected):
    from tenxgraph_api.cli.commands.api import _max_request_size

    if raw is None:
        monkeypatch.delenv("MAX_REQUEST_SIZE", raising=False)
    else:
        monkeypatch.setenv("MAX_REQUEST_SIZE", raw)
    assert _max_request_size() == expected


def test_build_command_refuses_compose_together_with_k8s(tmp_path, monkeypatch, silent_output):
    monkeypatch.chdir(tmp_path)
    cmd = BuildCommand(output=silent_output)
    code = cmd.execute(output_file="Dockerfile", force=True, docker_compose=True, k8s=True)
    assert code != 0
    assert list(tmp_path.iterdir()) == []
