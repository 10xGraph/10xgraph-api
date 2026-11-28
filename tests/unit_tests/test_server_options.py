"""TENXGRAPH_LOOP and TENXGRAPH_HTTP: parsing and where they are applied."""

# ruff: noqa: S101

import pytest

from tenxgraph_api.src.app import serve
from tenxgraph_api.src.app.core.config import server_options as opts


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TENXGRAPH_LOOP", raising=False)
    monkeypatch.delenv("TENXGRAPH_HTTP", raising=False)


def test_defaults_are_auto():
    assert opts.event_loop_choice() == "auto"
    assert opts.http_parser_choice() == "auto"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_means_default(monkeypatch, value):
    monkeypatch.setenv("TENXGRAPH_LOOP", value)
    monkeypatch.setenv("TENXGRAPH_HTTP", value)
    assert (opts.event_loop_choice(), opts.http_parser_choice()) == ("auto", "auto")


@pytest.mark.parametrize("value", ["auto", "asyncio", "uvloop"])
def test_valid_loop_values(monkeypatch, value):
    monkeypatch.setenv("TENXGRAPH_LOOP", value)
    assert opts.event_loop_choice() == value


@pytest.mark.parametrize("value", ["auto", "h11", "httptools"])
def test_valid_http_values(monkeypatch, value):
    monkeypatch.setenv("TENXGRAPH_HTTP", value)
    assert opts.http_parser_choice() == value


def test_case_and_whitespace_ignored(monkeypatch):
    monkeypatch.setenv("TENXGRAPH_LOOP", "  UvLoop ")
    monkeypatch.setenv("TENXGRAPH_HTTP", "\tH11\n")
    assert (opts.event_loop_choice(), opts.http_parser_choice()) == ("uvloop", "h11")


def test_invalid_loop_names_variable_and_values(monkeypatch):
    monkeypatch.setenv("TENXGRAPH_LOOP", "trio")
    with pytest.raises(ValueError, match="TENXGRAPH_LOOP.*auto, asyncio, uvloop"):
        opts.event_loop_choice()


def test_invalid_http_names_variable_and_values(monkeypatch):
    monkeypatch.setenv("TENXGRAPH_HTTP", "h2")
    with pytest.raises(ValueError, match="TENXGRAPH_HTTP.*auto, h11, httptools"):
        opts.http_parser_choice()


def test_build_config_honours_env(monkeypatch):
    monkeypatch.setenv("TENXGRAPH_LOOP", "asyncio")
    monkeypatch.setenv("TENXGRAPH_HTTP", "h11")
    config = serve.build_config("127.0.0.1", 8000)
    assert (config.loop, config.http) == ("asyncio", "h11")


def test_build_config_refuses_invalid_value(monkeypatch):
    monkeypatch.setenv("TENXGRAPH_LOOP", "nope")
    with pytest.raises(ValueError, match="TENXGRAPH_LOOP"):
        serve.build_config("127.0.0.1", 8000)


def test_worker_config_kwargs_honour_env(monkeypatch):
    import importlib

    from tenxgraph_api.src.app import uvicorn_worker

    monkeypatch.setenv("TENXGRAPH_LOOP", "asyncio")
    monkeypatch.setenv("TENXGRAPH_HTTP", "h11")
    try:
        reloaded = importlib.reload(uvicorn_worker)
        kwargs = reloaded.TenxGraphUvicornWorker.CONFIG_KWARGS
        assert (kwargs["loop"], kwargs["http"]) == ("asyncio", "h11")
    finally:
        monkeypatch.delenv("TENXGRAPH_LOOP")
        monkeypatch.delenv("TENXGRAPH_HTTP")
        importlib.reload(uvicorn_worker)
