from pydantic import BaseModel

from tenxgraph_api.src.app.core.config.settings import Settings
from tenxgraph_api.src.app.utils.parse_output import parse_message_output, parse_state_output


class StateModel(BaseModel):
    a: int
    b: int
    execution_meta: str | None = None


class MessageModel(BaseModel):
    text: str
    raw: dict | None = None


def test_parse_state_output_debug_true(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    settings = Settings()
    model = StateModel(a=1, b=2, execution_meta="meta")
    out = parse_state_output(settings, model)
    assert out == {"a": 1, "b": 2, "execution_meta": "meta"}


def test_parse_state_output_debug_false(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    settings = Settings()
    model = StateModel(a=1, b=2, execution_meta="meta")
    out = parse_state_output(settings, model)
    assert out == {"a": 1, "b": 2, "execution_meta": "meta"}


def test_parse_message_output_debug_true(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    settings = Settings()
    model = MessageModel(text="hello", raw={"tokens": 3})
    out = parse_message_output(settings, model)
    assert out == {"text": "hello"}


def test_parse_message_output_debug_false(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    settings = Settings()
    model = MessageModel(text="hello", raw={"tokens": 3})
    out = parse_message_output(settings, model)
    assert out == {"text": "hello"}


class _MetaState(BaseModel):
    execution_meta: dict


def _error_state() -> _MetaState:
    secret = "[NODE_001] connection to db.internal:5432 failed: password authentication"
    return _MetaState(execution_meta={"internal_data": {"error": secret, "trace_id": "t"}})


def test_parse_state_output_hides_error_text_in_production(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    settings = Settings(MODE="production", STRICT_PRODUCTION_CHECKS=False)

    out = parse_state_output(settings, _error_state())

    error = out["execution_meta"]["internal_data"]["error"]
    assert "db.internal" not in error
    assert "password" not in error
    assert out["execution_meta"]["internal_data"].keys() == {"error"}


def test_parse_state_output_keeps_error_text_in_development(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    settings = Settings(MODE="development")

    out = parse_state_output(settings, _error_state())

    assert "db.internal" in out["execution_meta"]["internal_data"]["error"]
