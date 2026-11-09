"""`10xgraph init` Full mode: presets, template blocks, flags."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.commands.init_context import (
    build_config,
    build_context,
    strip_blocks,
    template_flags,
    template_vars,
)
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.exceptions import FileOperationError, ValidationError


def _full(**options) -> dict:
    return build_context(path=".", agent_name="demo", mode="full", **options)


def _init(path: Path, **options) -> tuple[int, str]:
    stream = io.StringIO()
    code = InitCommand(OutputFormatter(stream=stream)).execute(
        path=str(path), agent_name="Demo", non_interactive=True, **options
    )
    return code, stream.getvalue()


# --- storage presets -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("storage", "cache", "store_key", "core_extras"),
    [
        ("memory", "memory", False, "google-genai"),
        ("sqlite", "memory", False, "google-genai,sqlite_checkpoint"),
        ("postgres", "redis", False, "google-genai,pg_checkpoint"),
        ("postgres-memory", "redis", True, "google-genai,pg_checkpoint,pgvector"),
    ],
)
def test_storage_preset(storage: str, cache: str, store_key: bool, core_extras: str) -> None:
    context = _full(storage=storage)
    assert context["storage"] == storage
    assert context["cache_store"] == cache
    config = build_config(context, True)
    assert config["cache_store"]["type"] == cache
    assert ("store" in config) is store_key
    if store_key:
        assert config["store"] == "graph.agent:store"
    assert template_vars(context)["core_requirement"] == f"10xgraph[{core_extras}]"


def test_cache_store_flag_overrides_the_preset() -> None:
    assert _full(storage="postgres", cache_store="memory")["cache_store"] == "memory"


def test_template_flags_follow_storage_and_provider() -> None:
    flags = template_flags(_full(storage="postgres-memory", provider="anthropic"))
    assert flags["postgres"] and flags["redis"] and flags["store"]
    assert flags["embed_openai"] and flags["embed_key"] and not flags["embed_google"]
    assert template_flags(_full(provider="google", storage="postgres-memory"))["embed_google"]
    flags = template_flags(_full(storage="memory"))
    assert flags["inmem"] and not flags["redis"] and not flags["store"]
    assert template_flags(_full(storage="sqlite"))["sqlite"]


# --- security presets ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("auth", "rate_limit", "revocation", "by"),
    [("jwt", True, True, "user"), ("none", True, False, "ip"), ("custom", True, True, "user")],
)
def test_security_preset(auth: str, rate_limit: bool, revocation: bool, by: str) -> None:
    context = _full(auth=auth)
    assert (context["auth"], context["rate_limit"], context["token_revocation"]) == (
        auth,
        rate_limit,
        revocation,
    )
    assert context["rl_by"] == by
    store = build_config(context, True)["cache_store"]
    assert store["rate_limit"]["by"] == by
    assert ("token_revocation" in store) is revocation


def test_full_defaults() -> None:
    context = _full()
    assert (context["storage"], context["auth"], context["cache_store"]) == (
        "memory",
        "jwt",
        "memory",
    )
    assert (context["evals"], context["tests"]) == (True, True)
    assert not any(context[k] for k in ("websocket", "ag_ui", "otel"))


# --- extras --------------------------------------------------------------------------


def test_extras_flags_skip_template_dirs_and_set_config(tmp_path: Path) -> None:
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    off, on = tmp_path / "off", tmp_path / "on"
    assert command.execute(path=str(off), mode="full", evals=False, tests=False, yes=True) == 0
    assert not (off / "evals").exists() and not (off / "tests").exists()
    assert (off / "graph" / "agent.py").exists()

    assert (
        command.execute(path=str(on), mode="full", websocket=True, ag_ui=True, otel=True, yes=True)
        == 0
    )
    assert (on / "evals").is_dir() and (on / "tests").is_dir()
    config = json.loads((on / "10xgraph.json").read_text())
    assert config["websocket"] == {"enabled": True}
    assert config["ag_ui"]["enabled"] is True
    api = template_vars(_full(websocket=True, ag_ui=True, otel=True))["api_requirement"]
    assert api == "10xgraph-api[jwt,websocket,ag-ui,otel]"


# --- strip_blocks --------------------------------------------------------------------


def test_strip_blocks_keeps_true_and_drops_false() -> None:
    text = "a\n##[IF_JWT]##\nb\n##[/IF_JWT]##\n##[IF_REDIS]##\nc\n##[/IF_REDIS]##\nd\n"
    assert strip_blocks(text, {"jwt": True, "redis": False}) == "a\nb\nd\n"


def test_strip_blocks_handles_comment_prefix_and_nesting() -> None:
    text = "# ##[IF_A]##\nx\n# ##[IF_B]##\ny\n# ##[/IF_B]##\n# ##[/IF_A]##\nz\n"
    assert strip_blocks(text, {"a": True, "b": True}) == "x\ny\nz\n"
    assert strip_blocks(text, {"a": True, "b": False}) == "x\nz\n"
    assert strip_blocks(text, {"a": False, "b": True}) == "z\n"


@pytest.mark.parametrize(
    "text",
    [
        "##[IF_TYPO]##\nx\n##[/IF_TYPO]##\n",
        "##[IF_JWT]##\nx\n",
        "x\n##[/IF_JWT]##\n",
        "##[IF_JWT]##\n##[IF_REDIS]##\n##[/IF_JWT]##\n##[/IF_REDIS]##\n",
        "value ##[IF_JWT]## inline ##[/IF_JWT]##\n",
    ],
)
def test_strip_blocks_rejects_bad_markers(text: str) -> None:
    with pytest.raises(FileOperationError):
        strip_blocks(text, {"jwt": True, "redis": True})


# --- flags and validation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "choices"),
    [
        ({"mode": "huge"}, "quick, full"),
        ({"storage": "mongo"}, "memory, sqlite, postgres, postgres-memory"),
        ({"auth": "basic"}, "none, jwt, custom"),
        ({"cache_store": "valkey"}, "memory, redis"),
        ({"provider": "mistral"}, "google, openai, anthropic"),
    ],
)
def test_invalid_values_list_the_choices(options: dict, choices: str) -> None:
    with pytest.raises(ValidationError) as exc:
        build_context(path=".", agent_name="demo", **{"mode": "full", **options})
    assert choices in exc.value.message


def test_invalid_template_and_conflicting_mode() -> None:
    with pytest.raises(ValidationError):
        build_context(path=".", agent_name="demo", template="enterprise")
    with pytest.raises(ValidationError):
        build_context(path=".", agent_name="demo", mode="quick", template="production")


def test_quick_rejects_full_only_options() -> None:
    for option in ({"storage": "sqlite"}, {"evals": True}, {"ag_ui": True}, {"otel": True}):
        with pytest.raises(ValidationError, match="--mode full"):
            build_context(path=".", agent_name="demo", **option)


def test_template_aliases() -> None:
    assert build_context(path=".", agent_name="d", template="production")["setup_type"] == (
        "production"
    )
    assert build_context(path=".", agent_name="d", template="quick-start")["setup_type"] == (
        "quick_start"
    )


def test_yes_is_quick_unless_mode_full(tmp_path: Path) -> None:
    command = InitCommand(OutputFormatter(stream=io.StringIO()))
    quick, full = tmp_path / "quick", tmp_path / "full"
    assert command.execute(path=str(quick), yes=True) == 0
    assert command.execute(path=str(full), mode="full", yes=True) == 0
    assert not (quick / "evals").exists() and (full / "evals").is_dir()
    assert json.loads((quick / "10xgraph.json").read_text())["auth"] is None
    assert json.loads((full / "10xgraph.json").read_text())["auth"] == {"method": "jwt"}


def test_invalid_storage_fails_the_command(tmp_path: Path) -> None:
    code, text = _init(tmp_path / "x", mode="full", storage="mongo")
    assert code != 0
    assert "memory, sqlite, postgres, postgres-memory" in text
    assert not (tmp_path / "x").exists()


def test_dry_run_lists_files_by_extras(tmp_path: Path) -> None:
    code, text = _init(tmp_path / "d", mode="full", evals=False, tests=True, dry_run=True)
    assert code == 0
    assert "Files that would be created" in text
    assert "evals/" not in text and "tests/" in text
    assert not (tmp_path / "d").exists()


def test_anthropic_store_adds_openai_extra_for_embeddings() -> None:
    context = _full(storage="postgres-memory", provider="anthropic")
    assert (
        template_vars(context)["core_requirement"]
        == "10xgraph[anthropic,pg_checkpoint,pgvector,openai]"
    )
    openai = _full(storage="postgres-memory", provider="openai")
    assert template_vars(openai)["core_requirement"] == "10xgraph[openai,pg_checkpoint,pgvector]"


@pytest.mark.parametrize(("tests", "evals"), [(True, True), (True, False), (False, True)])
def test_config_test_and_evaluation_sections_follow_flags(tests: bool, evals: bool) -> None:
    config = build_config(_full(tests=tests, evals=evals), True)
    assert ("test" in config) is tests
    assert ("evaluation" in config) is evals
    if tests:
        assert config["test"] == {"path": "tests", "coverage": True, "coverage_threshold": 70}
    if evals:
        assert config["evaluation"]["threshold"] == 0.75
        assert config["evaluation"]["directory"] == "evals"
