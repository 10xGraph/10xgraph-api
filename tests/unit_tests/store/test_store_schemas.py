"""Unit tests for store schemas."""

import pytest
from pydantic import ValidationError
from tenxgraph.core.state import Message
from tenxgraph.storage.store.store_schema import DistanceMetric, MemoryType, RetrievalStrategy

from tenxgraph_api.src.app.routers.store.schemas.store_schemas import (
    DeleteMemorySchema,
    ForgetMemorySchema,
    SearchMemorySchema,
    StoreMemorySchema,
    UpdateMemorySchema,
)


class TestStoreMemorySchema:
    """Tests for StoreMemorySchema validation."""

    def test_valid_with_string_content(self):
        """Test schema with valid string content."""
        schema = StoreMemorySchema(
            content="Test memory content",
            memory_type=MemoryType.EPISODIC,
            category="general",
            metadata={"key": "value"},
        )
        assert schema.content == "Test memory content"
        assert schema.memory_type == MemoryType.EPISODIC
        assert schema.category == "general"
        assert schema.metadata == {"key": "value"}

    def test_valid_with_message_content(self):
        """Test schema with Message content."""
        message = Message.text_message(role="user", content="Test message")
        schema = StoreMemorySchema(content=message)
        assert schema.content == message
        assert schema.memory_type == MemoryType.EPISODIC  # default
        assert schema.category == "general"  # default

    def test_defaults(self):
        """Test default values."""
        schema = StoreMemorySchema(content="Test")
        assert schema.memory_type == MemoryType.EPISODIC
        assert schema.category == "general"
        assert schema.metadata is None
        assert schema.config == {}
        assert schema.options is None

    def test_with_config_and_options(self):
        """Test schema with config and options."""
        schema = StoreMemorySchema(
            content="Test",
            config={"model": "custom"},
            options={"timeout": 30},
        )
        assert schema.config == {"model": "custom"}
        assert schema.options == {"timeout": 30}

    def test_missing_content_raises_error(self):
        """Test that missing content raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            StoreMemorySchema()
        errors = exc_info.value.errors()
        assert any(err["loc"] == ("content",) for err in errors)

    def test_all_memory_types(self):
        """Test all valid memory types."""
        for mem_type in MemoryType:
            schema = StoreMemorySchema(content="Test", memory_type=mem_type)
            assert schema.memory_type == mem_type


class TestSearchMemorySchema:
    """Tests for SearchMemorySchema validation."""

    def test_valid_basic_search(self):
        """Test valid basic search schema."""
        schema = SearchMemorySchema(query="test query")
        assert schema.query == "test query"
        assert schema.memory_type is None
        assert schema.category is None
        assert schema.limit == 10
        assert schema.score_threshold is None

    def test_with_all_filters(self):
        """Test schema with all filter options."""
        schema = SearchMemorySchema(
            query="test query",
            memory_type=MemoryType.SEMANTIC,
            category="work",
            limit=20,
            score_threshold=0.8,
            filters={"tag": "important"},
        )
        assert schema.query == "test query"
        assert schema.memory_type == MemoryType.SEMANTIC
        assert schema.category == "work"
        assert schema.limit == 20
        assert schema.score_threshold == 0.8
        assert schema.filters == {"tag": "important"}

    def test_with_retrieval_options(self):
        """Test schema with retrieval strategy options."""
        schema = SearchMemorySchema(
            query="test query",
            retrieval_strategy=RetrievalStrategy.HYBRID,
            distance_metric=DistanceMetric.EUCLIDEAN,
            max_tokens=2000,
        )
        assert schema.retrieval_strategy == RetrievalStrategy.HYBRID
        assert schema.distance_metric == DistanceMetric.EUCLIDEAN
        assert schema.max_tokens == 2000

    def test_default_values(self):
        """Test default values."""
        schema = SearchMemorySchema(query="test")
        assert schema.limit == 10
        # None = the store's own configured strategy and metric (core three-level settings)
        assert schema.retrieval_strategy is None
        assert schema.distance_metric is None
        assert schema.max_tokens == 4000

    def test_missing_query_raises_error(self):
        """Test that missing query raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            SearchMemorySchema()
        errors = exc_info.value.errors()
        assert any(err["loc"] == ("query",) for err in errors)

    def test_invalid_limit_raises_error(self):
        """Test that invalid limit raises validation error."""
        with pytest.raises(ValidationError):
            SearchMemorySchema(query="test", limit=0)

        with pytest.raises(ValidationError):
            SearchMemorySchema(query="test", limit=-1)

    def test_invalid_max_tokens_raises_error(self):
        """Test that invalid max_tokens raises validation error."""
        with pytest.raises(ValidationError):
            SearchMemorySchema(query="test", max_tokens=0)


class TestUpdateMemorySchema:
    """Tests for UpdateMemorySchema validation."""

    def test_valid_with_string_content(self):
        """Test schema with string content."""
        schema = UpdateMemorySchema(
            content="Updated content",
            metadata={"updated": True},
        )
        assert schema.content == "Updated content"
        assert schema.metadata == {"updated": True}

    def test_valid_with_message_content(self):
        """Test schema with Message content."""
        message = Message.text_message(role="assistant", content="Updated message")
        schema = UpdateMemorySchema(content=message)
        assert schema.content == message

    def test_with_config_and_options(self):
        """Test schema with config and options."""
        schema = UpdateMemorySchema(
            content="Updated",
            config={"version": 2},
            options={"force": True},
        )
        assert schema.config == {"version": 2}
        assert schema.options == {"force": True}

    def test_metadata_optional(self):
        """Test that metadata is optional."""
        schema = UpdateMemorySchema(content="Updated")
        assert schema.metadata is None

    def test_missing_content_raises_error(self):
        """Test that missing content raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            UpdateMemorySchema()
        errors = exc_info.value.errors()
        assert any(err["loc"] == ("content",) for err in errors)


class TestDeleteMemorySchema:
    """Tests for DeleteMemorySchema validation."""

    def test_valid_empty_schema(self):
        """Test valid empty schema."""
        schema = DeleteMemorySchema()
        assert schema.config == {}
        assert schema.options is None

    def test_with_config(self):
        """Test schema with config."""
        schema = DeleteMemorySchema(config={"soft_delete": True})
        assert schema.config == {"soft_delete": True}

    def test_with_options(self):
        """Test schema with options."""
        schema = DeleteMemorySchema(options={"force": True})
        assert schema.options == {"force": True}


class TestForgetMemorySchema:
    """Tests for ForgetMemorySchema validation."""

    def test_valid_with_memory_type(self):
        """Test schema with memory type."""
        schema = ForgetMemorySchema(memory_type=MemoryType.EPISODIC)
        assert schema.memory_type == MemoryType.EPISODIC
        assert schema.category is None
        assert schema.filters is None

    def test_valid_with_category(self):
        """Test schema with category."""
        schema = ForgetMemorySchema(category="work")
        assert schema.memory_type is None
        assert schema.category == "work"
        assert schema.filters is None

    def test_valid_with_filters(self):
        """Test schema with filters."""
        schema = ForgetMemorySchema(filters={"tag": "old"})
        assert schema.filters == {"tag": "old"}

    def test_with_all_fields(self):
        """Test schema with all fields."""
        schema = ForgetMemorySchema(
            memory_type=MemoryType.SEMANTIC,
            category="personal",
            filters={"age": ">30"},
            config={"dry_run": True},
            options={"verbose": True},
        )
        assert schema.memory_type == MemoryType.SEMANTIC
        assert schema.category == "personal"
        assert schema.filters == {"age": ">30"}
        assert schema.config == {"dry_run": True}
        assert schema.options == {"verbose": True}

    def test_defaults(self):
        """Test default values."""
        schema = ForgetMemorySchema()
        assert schema.memory_type is None
        assert schema.category is None
        assert schema.filters is None
        assert schema.config == {}
        assert schema.options is None


class TestBaseConfigSchema:
    """Tests for BaseConfigSchema behavior inherited by all schemas."""

    def test_config_default_factory(self):
        """Test that config uses default factory."""
        schema1 = StoreMemorySchema(content="test1")
        schema2 = StoreMemorySchema(content="test2")
        # Ensure they don't share the same dict instance
        schema1.config["key"] = "value1"
        assert "key" not in schema2.config

    def test_options_is_none_by_default(self):
        """Test that options defaults to None, not empty dict."""
        schema = StoreMemorySchema(content="test")
        assert schema.options is None


class TestSchemaEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_string_content(self):
        """Test that empty string content is valid."""
        schema = StoreMemorySchema(content="")
        assert schema.content == ""

    def test_large_metadata(self):
        """Test schema with large metadata."""
        large_metadata = {f"key_{i}": f"value_{i}" for i in range(100)}
        schema = StoreMemorySchema(content="test", metadata=large_metadata)
        assert len(schema.metadata) == 100

    def test_nested_filters(self):
        """Test schema with nested filter structure."""
        nested_filters = {
            "and": [
                {"tag": "important"},
                {"or": [{"category": "work"}, {"category": "urgent"}]},
            ]
        }
        schema = SearchMemorySchema(query="test", filters=nested_filters)
        assert schema.filters == nested_filters

    def test_unicode_content(self):
        """Test schema with unicode content."""
        unicode_content = "Test with émojis 🎉 and special chars: 你好"
        schema = StoreMemorySchema(content=unicode_content)
        assert schema.content == unicode_content

    def test_very_long_content(self):
        """Test schema with very long content."""
        long_content = "a" * 10000
        schema = StoreMemorySchema(content=long_content)
        assert len(schema.content) == 10000

    def test_score_threshold_boundaries(self):
        """Test score threshold with boundary values."""
        # Valid values
        schema1 = SearchMemorySchema(query="test", score_threshold=0.0)
        assert schema1.score_threshold == 0.0

        schema2 = SearchMemorySchema(query="test", score_threshold=1.0)
        assert schema2.score_threshold == 1.0

        # Note: Pydantic doesn't enforce bounds unless specified in Field
        schema3 = SearchMemorySchema(query="test", score_threshold=1.5)
        assert schema3.score_threshold == 1.5
