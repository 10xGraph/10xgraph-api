"""Tests for thread name generation utilities."""

import pytest

from tenxgraph_api.src.app.utils.thread_name_generator import (
    RandomThreadNameGenerator,
)


class TestRandomThreadNameGeneratorSimpleName:
    """Tests for RandomThreadNameGenerator.generate_simple_name method."""

    def test_generate_simple_name_default_separator(self):
        """Test generating simple name with default separator."""
        generator = RandomThreadNameGenerator()
        name = generator.generate_simple_name()

        # Should contain one hyphen
        assert "-" in name
        # Should have two parts
        assert len(name.split("-")) == 2







class TestRandomThreadNameGeneratorActionName:
    """Tests for RandomThreadNameGenerator.generate_action_name method."""

    def test_generate_action_name_default_separator(self):
        """Test generating action name with default separator."""
        generator = RandomThreadNameGenerator()
        name = generator.generate_action_name()

        # Should contain hyphen
        assert "-" in name
        # Should have action and target
        assert len(name.split("-")) == 2






class TestRandomThreadNameGeneratorCompoundName:
    """Tests for RandomThreadNameGenerator.generate_compound_name method."""

    def test_generate_compound_name_default_separator(self):
        """Test generating compound name with default separator."""
        generator = RandomThreadNameGenerator()
        name = generator.generate_compound_name()

        # Should contain hyphen
        assert "-" in name
        # Should have base and complement
        assert len(name.split("-")) == 2






class TestRandomThreadNameGeneratorGenerateName:
    """Tests for RandomThreadNameGenerator.generate_name method."""

    def test_generate_name_returns_string(self):
        """Test that generate_name returns a string."""
        generator = RandomThreadNameGenerator()
        name = generator.random_name()
        assert isinstance(name, str)







class TestRandomThreadNameGenerator:
    """Tests for RandomThreadNameGenerator."""

    @pytest.mark.asyncio
    async def test_default_generate_name_returns_string(self):
        """Test that RandomThreadNameGenerator.generate_name returns a string."""
        generator = RandomThreadNameGenerator()
        name = await generator.generate_name([])
        assert isinstance(name, str)





