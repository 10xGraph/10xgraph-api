"""The legacy OTEL_ENABLED gRPC exporter reports a down collector once."""

import enum
import logging
import sys
import types
from types import SimpleNamespace

import pytest


pytest.importorskip("opentelemetry.sdk")

from tenxgraph_api.src.app.core.config import observability  # noqa: E402


class _Result(enum.Enum):
    SUCCESS = 0
    FAILURE = 1


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    return mod


def test_grpc_exporter_failures_logged_once(monkeypatch, caplog):
    class FakeGrpcExporter:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        def export(self, spans):
            return _Result.FAILURE

    class FakeProvider:
        def __init__(self, resource=None):
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    class FakeBatchProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    fakes = {
        "opentelemetry.sdk": _module("opentelemetry.sdk"),
        "opentelemetry.sdk.resources": _module(
            "opentelemetry.sdk.resources", Resource=SimpleNamespace(create=lambda attrs: attrs)
        ),
        "opentelemetry.sdk.trace": _module("opentelemetry.sdk.trace", TracerProvider=FakeProvider),
        "opentelemetry.sdk.trace.export": _module(
            "opentelemetry.sdk.trace.export",
            BatchSpanProcessor=FakeBatchProcessor,
            SpanExportResult=_Result,
        ),
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": _module(
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
            OTLPSpanExporter=FakeGrpcExporter,
        ),
    }
    for name, mod in fakes.items():
        monkeypatch.setitem(sys.modules, name, mod)
    installed = []
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", installed.append)
    monkeypatch.setattr(observability, "_setup_otel_metrics", lambda *a, **k: None)

    settings = SimpleNamespace(
        OTEL_SERVICE_NAME="t", OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317", MODE="dev"
    )
    caplog.set_level(logging.INFO, logger="tenxgraph.trace_export")
    assert observability.setup_otel_provider(settings) is True

    exporter = installed[0].processors[0].exporter
    for _ in range(3):
        exporter.export([])
    records = [r for r in caplog.records if r.name == "tenxgraph.trace_export"]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "to otlp-grpc failed" in records[0].getMessage()
