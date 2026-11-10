"""Audit sinks: publisher sink, custom ``module:Class`` sinks and the sink error counter."""

# ruff: noqa: S101

import logging
import sys
import types
from typing import Any

import pytest
from tenxgraph.runtime.publisher.base_publisher import BasePublisher
from tenxgraph.utils.background_task_manager import BackgroundTaskManager
from tenxgraph.utils.metrics import counter

from tenxgraph_api import AuditEvent, BaseAuditSink
from tenxgraph_api.src.app.core.audit import _sink_failed
from tenxgraph_api.src.app.core.audit import sinks as sinks_module
from tenxgraph_api.src.app.core.audit.sinks import PublisherAuditSink, build_sinks


COUNTER = "tenxgraph_audit_sink_errors_total"


class _Publisher(BasePublisher):
    def __init__(self, kind: str = "events") -> None:
        super().__init__({})
        self.kind = kind  # type: ignore[assignment]

    async def publish(self, event):
        return None

    async def close(self):
        return None

    def sync_close(self):
        return None


class _Container:
    """Stands in for ``InjectQ.get_instance()``."""

    publisher: BasePublisher | None = None

    @classmethod
    def get_instance(cls):
        return cls

    @classmethod
    def try_get(cls, key):
        return cls.publisher


class _Mine(BaseAuditSink):
    async def emit(self, event: AuditEvent) -> None:
        return None


_INSTANCE = _Mine()


def _factory() -> BaseAuditSink:
    return _Mine()


def _make_module(monkeypatch, **attrs: Any) -> None:
    module = types.ModuleType("audit_sink_fixture")
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "audit_sink_fixture", module)


@pytest.fixture
def container(monkeypatch):
    _Container.publisher = None
    monkeypatch.setattr(sinks_module, "InjectQ", _Container)
    yield _Container
    _Container.publisher = None


def _event() -> AuditEvent:
    return AuditEvent(event="change", result="success", action="threads:delete", user_id="u1")


# -- publisher --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publisher_sink_sends_the_event_dump(container, monkeypatch):
    sent: list[dict] = []

    def fake_publish(event, publisher, task_manager, only):
        sent.append({"event": event, "publisher": publisher, "only": only, "tm": task_manager})

    monkeypatch.setattr(sinks_module, "publish_event", fake_publish)
    container.publisher = publisher = _Publisher()
    [sink] = build_sinks(["publisher"])
    assert isinstance(sink, PublisherAuditSink)
    record = _event()
    await sink.emit(record)
    assert len(sent) == 1
    assert sent[0]["publisher"] is publisher
    assert sent[0]["only"] == "events"
    assert isinstance(sent[0]["tm"], BackgroundTaskManager)
    assert sent[0]["tm"] is sink._tasks  # its own manager, not the container's
    assert sent[0]["event"].data == record.model_dump()
    assert sent[0]["event"].user_id == "u1"


def test_publisher_listed_without_publisher_warns_and_is_skipped(container, caplog):
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        assert build_sinks(["log", "publisher"])[0].__class__.__name__ == "LogAuditSink"
    assert len(build_sinks(["publisher"])) == 0
    assert any('"publisher"' in r.message and "skipped" in r.message for r in caplog.records)


def test_observability_only_publisher_is_skipped(container, caplog):
    container.publisher = _Publisher(kind="observability")
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        assert build_sinks(["publisher"]) == []
    assert any("skipped" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_publisher_sink_never_raises_into_the_request_path(container):
    """With a real publisher and no running manager the event is queued or shed, not raised."""
    container.publisher = _Publisher()
    [sink] = build_sinks(["publisher"])
    await sink.emit(_event())


# -- custom sinks -----------------------------------------------------------------


@pytest.mark.parametrize("attr", ["_INSTANCE", "_Mine", "_factory"])
def test_custom_sink_instance_subclass_or_factory(monkeypatch, attr):
    _make_module(monkeypatch, _INSTANCE=_INSTANCE, _Mine=_Mine, _factory=_factory)
    [sink] = build_sinks([f"audit_sink_fixture:{attr}"])
    assert isinstance(sink, _Mine)
    if attr == "_INSTANCE":
        assert sink is _INSTANCE


@pytest.mark.parametrize(
    "path, attrs",
    [
        ("no_such_audit_module_xyz:Sink", {}),
        ("audit_sink_fixture:Missing", {}),
        ("audit_sink_fixture:NotASink", {"NotASink": object}),
        ("audit_sink_fixture:Number", {"Number": 5}),
        ("audit_sink_fixture:bad_factory", {"bad_factory": lambda: "nope"}),
    ],
)
def test_bad_custom_sink_refuses_boot(monkeypatch, path, attrs):
    _make_module(monkeypatch, **attrs)
    with pytest.raises(ValueError, match="audit.sinks"):
        build_sinks([path])


# -- counter ----------------------------------------------------------------------


def test_sink_failure_increments_the_counter():
    before = counter(COUNTER).value
    _sink_failed(_Mine(), RuntimeError("down"))
    assert counter(COUNTER).value == before + 1
