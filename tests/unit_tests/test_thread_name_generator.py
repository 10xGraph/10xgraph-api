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

    def test_generate_simple_name_custom_separator(self):
        """Test generating simple name with custom separator."""
        generator = RandomThreadNameGenerator()
        name = generator.generate_simple_name(separator="_")

        # Should contain one underscore
        assert "_" in name
        # Should have two parts
        assert len(name.split("_")) == 2

    def test_generate_simple_name_space_separator(self):
        """Test generating simple name with space separator."""
        generator = RandomThreadNameGenerator()
        name = generator.generate_simple_name(separator=" ")

        # Should contain one space
        assert " " in name
        # Should have two parts
        assert len(name.split(" ")) == 2

    def test_generate_simple_name_contains_adjective(self):
        """Test that simple name contains a valid adjective."""
        generator = RandomThreadNameGenerator()

        for _ in range(10):
            name = generator.generate_simple_name()
            adj, noun = name.split("-")
            assert adj in generator.ADJECTIVES

    def test_generate_simple_name_contains_noun(self):
        """Test that simple name contains a valid noun."""
        generator = RandomThreadNameGenerator()

        for _ in range(10):
            name = generator.generate_simple_name()
            adj, noun = name.split("-")
            assert noun in generator.NOUNS

    def test_generate_simple_name_empty_separator(self):
        """Test generating simple name with empty separator."""
        generator = RandomThreadNameGenerator()
        name = generator.generate_simple_name(separator="")

        # Should have no separator
        assert "-" not in name
        # Should be composed of adjective + noun
        found = False
        for adj in generator.ADJECTIVES:
            for noun in generator.NOUNS:
                if adj + noun == name:
                    found = True
                    break
            if found:
                break
        assert found


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

    def test_generate_action_name_custom_separator(self):
        """Test generating action name with custom separator."""
        generator = RandomThreadNameGenerator()
        name = generator.generate_action_name(separator=":")

        # Should contain colon
        assert ":" in name
        # Should have action and target
        assert len(name.split(":")) == 2

    def test_generate_action_name_contains_valid_action(self):
        """Test that action name contains a valid action."""
        generator = RandomThreadNameGenerator()

        for _ in range(10):
            name = generator.generate_action_name()
            action, target = name.split("-")
            assert action in generator.ACTION_PATTERNS.keys()

    def test_generate_action_name_contains_valid_target(self):
        """Test that action name contains a valid target."""
        generator = RandomThreadNameGenerator()

        for _ in range(10):
            name = generator.generate_action_name()
            action, target = name.split("-")
            assert target in generator.ACTION_PATTERNS[action]

    def test_generate_action_name_variations(self):
        """Test that action names have good variation."""
        generator = RandomThreadNameGenerator()
        names = set()

        # Generate multiple names
        for _ in range(20):
            names.add(generator.generate_action_name())

        # Should have at least 5 different names (with high probability)
        assert len(names) >= 5


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

    def test_generate_compound_name_custom_separator(self):
        """Test generating compound name with custom separator."""
        generator = RandomThreadNameGenerator()
        name = generator.generate_compound_name(separator=".")

        # Should contain period
        assert "." in name
        # Should have base and complement
        assert len(name.split(".")) == 2

    def test_generate_compound_name_contains_valid_base(self):
        """Test that compound name contains a valid base."""
        generator = RandomThreadNameGenerator()

        valid_bases = [base for base, _ in generator.COMPOUND_PATTERNS]

        for _ in range(10):
            name = generator.generate_compound_name()
            base, complement = name.split("-")
            assert base in valid_bases

    def test_generate_compound_name_contains_valid_complement(self):
        """Test that compound name contains a valid complement."""
        generator = RandomThreadNameGenerator()
        pattern_dict = dict(generator.COMPOUND_PATTERNS)

        for _ in range(10):
            name = generator.generate_compound_name()
            base, complement = name.split("-")
            assert complement in pattern_dict[base]

    def test_generate_compound_name_variations(self):
        """Test that compound names have good variation."""
        generator = RandomThreadNameGenerator()
        names = set()

        # Generate multiple names
        for _ in range(30):
            names.add(generator.generate_compound_name())

        # Should have at least 10 different names (with high probability)
        assert len(names) >= 10


class TestRandomThreadNameGeneratorGenerateName:
    """Tests for RandomThreadNameGenerator.generate_name method."""

    def test_generate_name_returns_string(self):
        """Test that generate_name returns a string."""
        generator = RandomThreadNameGenerator()
        name = generator.random_name()
        assert isinstance(name, str)

    def test_generate_name_contains_separator(self):
        """Test that generate_name contains separator."""
        generator = RandomThreadNameGenerator()
        name = generator.random_name()
        assert "-" in name

    def test_generate_name_default_separator(self):
        """Test generate_name with default separator."""
        generator = RandomThreadNameGenerator()
        name = generator.random_name()

        # Should have hyphen as default separator
        assert "-" in name

    def test_generate_name_custom_separator(self):
        """Test generate_name with custom separator."""
        generator = RandomThreadNameGenerator()
        name = generator.random_name(separator="_")

        # Should use custom separator
        assert "_" in name
        assert "-" not in name

    def test_generate_name_uses_different_patterns(self):
        """Test that generate_name uses different patterns."""
        generator = RandomThreadNameGenerator()

        # Generate multiple names to likely get all patterns
        names = []
        for _ in range(30):
            names.append(generator.random_name())

        # All should be valid strings with separators
        for name in names:
            assert isinstance(name, str)
            assert "-" in name
            parts = name.split("-")
            assert len(parts) >= 2

    def test_generate_name_variations(self):
        """Test that generate_name produces varied names."""
        generator = RandomThreadNameGenerator()
        names = set()

        # Generate many names
        for _ in range(50):
            names.add(generator.random_name())

        # Should have significant variation
        assert len(names) >= 20


class TestRandomThreadNameGenerator:
    """Tests for RandomThreadNameGenerator."""

    @pytest.mark.asyncio
    async def test_default_generate_name_returns_string(self):
        """Test that RandomThreadNameGenerator.generate_name returns a string."""
        generator = RandomThreadNameGenerator()
        name = await generator.generate_name([])
        assert isinstance(name, str)

    @pytest.mark.asyncio
    async def test_default_generate_name_ignores_messages(self):
        """Test that RandomThreadNameGenerator ignores input messages."""
        generator = RandomThreadNameGenerator()

        # Should work with any messages parameter
        name1 = await generator.generate_name([])
        name2 = await generator.generate_name(["message 1", "message 2"])
        name3 = await generator.generate_name(["very", "long", "list", "of", "messages"])

        assert isinstance(name1, str)
        assert isinstance(name2, str)
        assert isinstance(name3, str)

    @pytest.mark.asyncio
    async def test_default_generate_name_has_separator(self):
        """Test that RandomThreadNameGenerator uses separator."""
        generator = RandomThreadNameGenerator()
        name = await generator.generate_name([])

        # Should have hyphen as separator
        assert "-" in name

    @pytest.mark.asyncio
    async def test_default_generate_name_multiple_calls(self):
        """Test that RandomThreadNameGenerator generates different names."""
        generator = RandomThreadNameGenerator()

        names = set()
        for _ in range(20):
            name = await generator.generate_name([])
            names.add(name)

        # Should have multiple different names
        assert len(names) >= 5


class TestRandomThreadNameGeneratorEdgeCases:
    """Tests for edge cases in RandomThreadNameGenerator."""

    def test_generate_simple_name_none_separator(self):
        """Test generate_simple_name with None separator (uses default)."""
        generator = RandomThreadNameGenerator()
        # Should still work even if called in unexpected ways
        name = generator.generate_simple_name(separator="-")
        assert "-" in name

    def test_multiple_generators_independence(self):
        """Test that multiple generator instances are independent."""
        gen1 = RandomThreadNameGenerator()
        gen2 = RandomThreadNameGenerator()

        # Generate names from both
        name1 = gen1.random_name()
        name2 = gen2.random_name()

        # Both should be valid
        assert isinstance(name1, str)
        assert isinstance(name2, str)
        assert "-" in name1
        assert "-" in name2

    def test_generate_name_pattern_distribution(self):
        """Test that generate_name uses all three patterns reasonably."""
        generator = RandomThreadNameGenerator()

        # Track which patterns are used by looking at generated names
        names = []
        for _ in range(100):
            names.append(generator.random_name())

        # All should be valid
        assert len(names) == 100
        assert all("-" in name for name in names)
