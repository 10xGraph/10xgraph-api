"""``to_core_id``: thread and message ids in the type of the bound id generator."""

import pytest
from injectq import InjectQ
from tenxgraph.utils.id_generator import IDType

from tenxgraph_api.src.app.core.validation.request_config import ClientInputError
from tenxgraph_api.src.app.core.validation.resource_ids import to_core_id


@pytest.fixture
def bind_type(monkeypatch):
    """Make the container report an id type (a real generator is bound in the app tests)."""

    def bind(id_type):
        container = InjectQ()
        container.bind("generated_id_type", id_type)
        monkeypatch.setattr(InjectQ, "get_instance", classmethod(lambda cls: container))

    return bind


def test_string_generator_keeps_strings_and_writes_integers_as_text(bind_type):
    bind_type(IDType.STRING)
    assert to_core_id("abc-1") == "abc-1"
    assert to_core_id("123") == "123"
    assert to_core_id(123) == "123"
    assert to_core_id(None) is None


@pytest.mark.parametrize("id_type", [IDType.INTEGER, IDType.BIGINT])
def test_integer_generator_converts_numeric_strings(bind_type, id_type):
    bind_type(id_type)
    assert to_core_id("123") == 123
    assert to_core_id(" 45 ") == 45
    assert to_core_id(7) == 7
    assert to_core_id(None) is None


@pytest.mark.parametrize("bad", ["abc", "1.5", "-3", "", "12a", "١٢٣"])
def test_integer_generator_refuses_a_non_numeric_id(bind_type, bad):
    bind_type(IDType.BIGINT)
    with pytest.raises(ClientInputError, match="message_id must be an integer"):
        to_core_id(bad, "message_id")


def test_integer_ids_are_range_checked(bind_type):
    bind_type(IDType.INTEGER)
    with pytest.raises(ClientInputError, match="between 0 and"):
        to_core_id(2**31)
    bind_type(IDType.BIGINT)
    assert to_core_id(2**31) == 2**31
    with pytest.raises(ClientInputError):
        to_core_id(2**63)


def test_conversion_is_idempotent(bind_type):
    bind_type(IDType.BIGINT)
    assert to_core_id(to_core_id("99")) == 99
