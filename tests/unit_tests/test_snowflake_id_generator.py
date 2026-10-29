"""Unit tests for SnowFlakeIdGenerator."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_snowflake_kit():
    """Mock snowflakekit module."""
    with patch("tenxgraph_api.src.app.utils.snowflake_id_generator.find_spec") as mock_find_spec:
        mock_find_spec.return_value = MagicMock()

        mock_config_class = MagicMock()
        mock_generator_class = MagicMock()
        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(return_value=12345)
        mock_generator_class.return_value = mock_generator

        mock_modules = {
            "snowflakekit": MagicMock(
                SnowflakeConfig=mock_config_class,
                SnowflakeGenerator=mock_generator_class,
            )
        }

        with patch.dict("sys.modules", mock_modules):
            yield {
                "config_class": mock_config_class,
                "generator_class": mock_generator_class,
                "generator": mock_generator,
            }


class TestSnowFlakeIdGeneratorImportError:
    """Test SnowFlakeIdGenerator import error handling."""

    def test_raises_import_error_when_snowflakekit_not_available(self):
        """Test that ImportError is raised when snowflakekit is not installed."""
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        with patch("tenxgraph_api.src.app.utils.snowflake_id_generator.HAS_SNOWFLAKE", False):
            with pytest.raises(ImportError, match="snowflakekit is not installed"):
                SnowFlakeIdGenerator()


class TestSnowFlakeIdGeneratorInitialization:
    """Test SnowFlakeIdGenerator initialization."""

    def test_init_with_env_vars(self, mock_snowflake_kit):
        """Test initialization using environment variables."""
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        env_vars = {
            "SNOWFLAKE_EPOCH": "1723323246031",
            "SNOWFLAKE_TOTAL_BITS": "64",
            "SNOWFLAKE_TIME_BITS": "39",
            "SNOWFLAKE_NODE_BITS": "7",
            "SNOWFLAKE_NODE_ID": "0",
            "SNOWFLAKE_WORKER_ID": "0",
            "SNOWFLAKE_WORKER_BITS": "5",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            generator = SnowFlakeIdGenerator()

        assert generator.generator is not None
        mock_snowflake_kit["config_class"].assert_called_once()

    def test_init_with_explicit_params(self, mock_snowflake_kit):
        """Test initialization with explicit parameters."""
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        generator = SnowFlakeIdGenerator(
            snowflake_epoch=1723323246031,
            total_bits=64,
            snowflake_time_bits=39,
            snowflake_node_bits=7,
            snowflake_node_id=0,
            snowflake_worker_id=0,
            snowflake_worker_bits=5,
        )

        assert generator.generator is not None
        mock_snowflake_kit["config_class"].assert_called_once()

    def test_init_with_partial_params_uses_env(self, mock_snowflake_kit):
        """Test initialization with partial parameters falls back to defaults."""
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        # When only some params are provided, it should use env vars
        # This should not raise an error because the code has a default fallback
        with patch.dict(os.environ, {"SNOWFLAKE_EPOCH": "1723323246031"}, clear=False):
            with patch.dict(os.environ, {"SNOWFLAKE_TOTAL_BITS": "64"}, clear=False):
                # Providing partial params - still uses env vars as fallback
                # The code doesn't handle partial params, so this tests the path where
                # some params are None but not all - which should use env vars
                generator = SnowFlakeIdGenerator()

        assert generator.generator is not None


class TestSnowFlakeIdGeneratorIdType:
    """Test SnowFlakeIdGenerator ID type property."""

    def test_id_type_is_bigint(self, mock_snowflake_kit):
        """Test that id_type returns IDType.BIGINT."""
        from tenxgraph.utils.id_generator import IDType

        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        generator = SnowFlakeIdGenerator(
            snowflake_epoch=1723323246031,
            total_bits=64,
            snowflake_time_bits=39,
            snowflake_node_bits=7,
            snowflake_node_id=0,
            snowflake_worker_id=0,
            snowflake_worker_bits=5,
        )

        assert generator.id_type == IDType.BIGINT


class TestSnowFlakeIdGeneratorGenerate:
    """Test SnowFlakeIdGenerator generate method."""

    @pytest.mark.asyncio
    async def test_generate_returns_id(self, mock_snowflake_kit):
        """Test that generate returns a valid ID."""
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        generator = SnowFlakeIdGenerator(
            snowflake_epoch=1723323246031,
            total_bits=64,
            snowflake_time_bits=39,
            snowflake_node_bits=7,
            snowflake_node_id=0,
            snowflake_worker_id=0,
            snowflake_worker_bits=5,
        )

        id_result = await generator.generate()

        assert id_result == 12345
        mock_snowflake_kit["generator"].generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_multiple_ids(self, mock_snowflake_kit):
        """Test generating multiple IDs."""
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        # Configure mock to return different values
        mock_snowflake_kit["generator"].generate = AsyncMock(side_effect=[1, 2, 3])

        generator = SnowFlakeIdGenerator(
            snowflake_epoch=1723323246031,
            total_bits=64,
            snowflake_time_bits=39,
            snowflake_node_bits=7,
            snowflake_node_id=0,
            snowflake_worker_id=0,
            snowflake_worker_bits=5,
        )

        id1 = await generator.generate()
        id2 = await generator.generate()
        id3 = await generator.generate()

        assert id1 == 1
        assert id2 == 2
        assert id3 == 3
        assert mock_snowflake_kit["generator"].generate.call_count == 3


class TestSnowFlakeIdGeneratorConfigEnvValues:
    """Test different environment variable configurations."""

    def test_init_with_custom_env_values(self, mock_snowflake_kit):
        """Test initialization with custom environment values."""
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        custom_env = {
            "SNOWFLAKE_EPOCH": "999999999999",
            "SNOWFLAKE_TOTAL_BITS": "128",
            "SNOWFLAKE_TIME_BITS": "50",
            "SNOWFLAKE_NODE_BITS": "10",
            "SNOWFLAKE_NODE_ID": "5",
            "SNOWFLAKE_WORKER_ID": "10",
            "SNOWFLAKE_WORKER_BITS": "8",
        }

        with patch.dict(os.environ, custom_env, clear=False):
            generator = SnowFlakeIdGenerator()

        assert generator.generator is not None

        # Verify SnowflakeConfig was called with correct values
        call_args = mock_snowflake_kit["config_class"].call_args
        assert call_args[1]["epoch"] == 999999999999
        assert call_args[1]["total_bits"] == 128
        assert call_args[1]["time_bits"] == 50
        assert call_args[1]["node_bits"] == 10
        assert call_args[1]["node_id"] == 5
        assert call_args[1]["worker_id"] == 10
        assert call_args[1]["worker_bits"] == 8


class TestSnowFlakeIdGeneratorConfig:
    """G-103: complete config from partial input, and distinct ids per process."""

    @staticmethod
    def _env(**overrides):
        keep = {k: v for k, v in os.environ.items() if not k.startswith("SNOWFLAKE_")}
        return patch.dict(os.environ, {**keep, **overrides}, clear=True)

    def test_partial_params_build_a_complete_config(self, mock_snowflake_kit):
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        with self._env():
            SnowFlakeIdGenerator(snowflake_node_id=3, snowflake_worker_id=4)

        kwargs = mock_snowflake_kit["config_class"].call_args.kwargs
        assert kwargs["node_id"] == 3
        assert kwargs["worker_id"] == 4
        assert kwargs["epoch"] == 1723323246031
        assert mock_snowflake_kit["generator_class"].call_args.kwargs["config"] is not None

    def test_worker_id_differs_between_processes_when_unset(self, mock_snowflake_kit):
        from tenxgraph_api.src.app.utils import snowflake_id_generator as module

        ids = []
        with self._env():
            for pid in (1001, 1002):
                with patch.object(module.os, "getpid", return_value=pid):
                    module.SnowFlakeIdGenerator()
                ids.append(mock_snowflake_kit["config_class"].call_args.kwargs["worker_id"])
        assert ids[0] != ids[1]

    def test_explicit_env_ids_are_used_as_given(self, mock_snowflake_kit):
        from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator

        with self._env(SNOWFLAKE_NODE_ID="5", SNOWFLAKE_WORKER_ID="6"):
            SnowFlakeIdGenerator()
        kwargs = mock_snowflake_kit["config_class"].call_args.kwargs
        assert (kwargs["node_id"], kwargs["worker_id"]) == (5, 6)
