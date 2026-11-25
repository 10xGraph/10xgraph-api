"""Provider and exporter setup for the ``observability`` block (tracing_backends.py).

The core processor builders and the TracerProvider are mocked; logfire is a fake module.
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


pytest.importorskip("opentelemetry.sdk")

from tenxgraph_api.src.app.core.config import tracing_backends as tb  # noqa: E402


MOD = "tenxgraph_api.src.app.core.config.tracing_backends"


def _fake_logfire(configure=None):
    lf = types.ModuleType("logfire")
    lf.configure = configure or MagicMock()
    return lf


def _run(config, *, logfire=None, env=None):
    """Call configure_tracing_backends with the builders and provider mocked."""
    mods = {"logfire": logfire} if logfire is not None else {}
    with (
        patch.dict(sys.modules, mods),
        patch.dict("os.environ", env or {}, clear=True),
        patch(f"{MOD}._build_langsmith_processor") as ls,
        patch(f"{MOD}._build_phoenix_processor") as ph,
        patch(f"{MOD}._build_otlp_processor") as otlp,
        patch(f"{MOD}._new_tracer_provider") as new,
        patch("opentelemetry.trace.set_tracer_provider") as set_global,
    ):
        tb.configure_tracing_backends(config)
    return {"ls": ls, "ph": ph, "otlp": otlp, "new": new, "set": set_global}


def test_nothing_enabled_installs_nothing():
    m = _run({"logfire": {"enabled": False}, "langsmith": None})
    m["new"].assert_not_called()
    m["set"].assert_not_called()


def test_invalid_user_id_mode_raises_even_when_disabled():
    with pytest.raises(ValueError, match="raw, hash, mask"):
        tb.configure_tracing_backends({"user_id_mode": "bad"})


def test_langsmith_only_sets_one_global_provider():
    m = _run({"langsmith": {"enabled": True, "project": "proj", "endpoint": "https://x/otel"}})
    m["ls"].assert_called_once_with(project="proj", endpoint="https://x/otel")
    m["new"].assert_called_once_with(None)
    provider = m["new"].return_value
    provider.add_span_processor.assert_called_once_with(m["ls"].return_value)
    m["set"].assert_called_once_with(provider)


def test_langsmith_null_endpoint_uses_default():
    m = _run({"langsmith": {"enabled": True, "endpoint": None}})
    assert m["ls"].call_args.kwargs["endpoint"] == "https://api.smith.langchain.com/otel"


def test_langsmith_missing_key_raises():
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match="LANGSMITH_API_KEY"),
    ):
        tb.configure_tracing_backends({"langsmith": {"enabled": True}})


def test_langsmith_phoenix_otlp_share_one_provider_in_order():
    m = _run(
        {
            "langsmith": {"enabled": True},
            "phoenix": {"enabled": True, "project": "p", "endpoint": "http://h:6006"},
            "otlp": {"enabled": True, "endpoint": "http://c:4318", "extra": 1},
        }
    )
    m["ph"].assert_called_once_with(endpoint="http://h:6006")
    m["otlp"].assert_called_once_with(endpoint="http://c:4318")
    m["new"].assert_called_once_with("p")
    added = [c.args[0] for c in m["new"].return_value.add_span_processor.call_args_list]
    assert added == [m["ls"].return_value, m["ph"].return_value, m["otlp"].return_value]
    m["set"].assert_called_once()


def test_phoenix_project_from_env():
    m = _run({"phoenix": {"enabled": True}}, env={"PHOENIX_PROJECT": "envp"})
    m["new"].assert_called_once_with("envp")


def test_otlp_null_endpoint_passes_none():
    m = _run({"otlp": {"enabled": True, "endpoint": ""}})
    m["otlp"].assert_called_once_with(endpoint=None)


def test_logfire_only_configures_logfire_and_no_provider():
    lf = _fake_logfire()
    m = _run(
        {"logfire": {"enabled": True, "service_name": "svc", "send_to_logfire": False}},
        logfire=lf,
    )
    lf.configure.assert_called_once_with(send_to_logfire=False, service_name="svc")
    m["new"].assert_not_called()
    m["set"].assert_not_called()


def test_logfire_defaults_and_console():
    lf = _fake_logfire()
    _run({"logfire": {"enabled": True, "console": False}}, logfire=lf)
    lf.configure.assert_called_once_with(send_to_logfire=True, console=False)


def test_logfire_missing_raises_import_error():
    with (
        patch.dict(sys.modules, {"logfire": None}),
        pytest.raises(ImportError, match="logfire"),
    ):
        tb.configure_tracing_backends({"logfire": {"enabled": True}})


def test_logfire_and_langsmith_pass_processor_to_logfire():
    lf = _fake_logfire()
    m = _run({"logfire": {"enabled": True}, "langsmith": {"enabled": True}}, logfire=lf)
    kwargs = lf.configure.call_args.kwargs
    assert kwargs["additional_span_processors"] == [m["ls"].return_value]
    assert "resource_attributes" not in kwargs
    m["new"].assert_not_called()


def test_logfire_and_phoenix_pass_project_as_resource_attribute():
    seen = {}

    def configure(*, resource_attributes=None, **kwargs):
        seen.update(kwargs, resource_attributes=resource_attributes)

    m = _run(
        {"logfire": {"enabled": True}, "phoenix": {"enabled": True, "project": "p"}},
        logfire=_fake_logfire(configure),
    )
    assert seen["additional_span_processors"] == [m["ph"].return_value]
    assert seen["resource_attributes"] == {"openinference.project.name": "p"}


def test_logfire_without_resource_attributes_warns_and_omits(caplog):
    seen = {}

    def configure(**kwargs):
        seen.update(kwargs)

    with caplog.at_level("WARNING"):
        _run(
            {"logfire": {"enabled": True}, "phoenix": {"enabled": True, "project": "p"}},
            logfire=_fake_logfire(configure),
        )
    assert "resource_attributes" not in seen
    assert len(seen["additional_span_processors"]) == 1
    assert "OTEL_RESOURCE_ATTRIBUTES" in caplog.text


def test_logfire_and_phoenix_without_project_omits_resource_attributes(caplog):
    seen = {}

    def configure(*, resource_attributes=None, **kwargs):
        seen.update(kwargs, resource_attributes=resource_attributes)

    with caplog.at_level("WARNING"):
        _run(
            {"logfire": {"enabled": True}, "phoenix": {"enabled": True}},
            logfire=_fake_logfire(configure),
        )
    assert seen["resource_attributes"] is None
    assert "OTEL_RESOURCE_ATTRIBUTES" not in caplog.text
