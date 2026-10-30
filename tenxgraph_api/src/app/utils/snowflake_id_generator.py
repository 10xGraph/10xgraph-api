import logging
import os
import socket
import zlib
from importlib.util import find_spec
from typing import overload

from tenxgraph.utils.id_generator import BaseIDGenerator, IDType


# Check if snowflakekit is available
HAS_SNOWFLAKE = find_spec("snowflakekit") is not None

logger = logging.getLogger("tenxgraph_api.snowflake")


@overload
def _setting(value: int | None, env_name: str, default: int) -> int: ...
@overload
def _setting(value: int | None, env_name: str, default: None) -> int | None: ...
def _setting(value: int | None, env_name: str, default: int | None) -> int | None:
    """``value`` when given, else the environment variable, else ``default``."""
    if value is not None:
        return value
    raw = os.environ.get(env_name)
    return int(raw) if raw not in (None, "") else default


class SnowFlakeIdGenerator(BaseIDGenerator):
    def __init__(
        self,
        snowflake_epoch: int | None = None,
        total_bits: int | None = None,
        snowflake_time_bits: int | None = None,
        snowflake_node_bits: int | None = None,
        snowflake_node_id: int | None = None,
        snowflake_worker_id: int | None = None,
        snowflake_worker_bits: int | None = None,
    ):
        if not HAS_SNOWFLAKE:
            raise ImportError(
                "snowflakekit is not installed. Please install it to use SnowFlakeIdGenerator."
            )

        from snowflakekit import SnowflakeConfig, SnowflakeGenerator

        # Each value is, in order: the argument, its SNOWFLAKE_* environment variable, a default.
        # A partly given configuration therefore still builds a complete config.
        epoch = _setting(snowflake_epoch, "SNOWFLAKE_EPOCH", 1723323246031)
        total = _setting(total_bits, "SNOWFLAKE_TOTAL_BITS", 64)
        time_bits = _setting(snowflake_time_bits, "SNOWFLAKE_TIME_BITS", 39)
        node_bits = _setting(snowflake_node_bits, "SNOWFLAKE_NODE_BITS", 7)
        worker_bits = _setting(snowflake_worker_bits, "SNOWFLAKE_WORKER_BITS", 5)

        # Two processes with the same node and worker id mint the same ids. When the ids are
        # not set, derive them from this process (worker id from the pid, node id from the
        # host name) instead of giving every gunicorn worker the same 0/0.
        node_id = _setting(snowflake_node_id, "SNOWFLAKE_NODE_ID", None)
        if node_id is None:
            node_id = zlib.crc32(socket.gethostname().encode()) % (1 << node_bits)
            logger.warning(
                "SNOWFLAKE_NODE_ID is not set; using %d derived from the host name. "
                "Set it explicitly to keep ids unique across hosts.",
                node_id,
            )
        worker_id = _setting(snowflake_worker_id, "SNOWFLAKE_WORKER_ID", None)
        if worker_id is None:
            worker_id = os.getpid() % (1 << worker_bits)
            logger.warning(
                "SNOWFLAKE_WORKER_ID is not set; using %d derived from the process id. "
                "Set it per worker to rule out collisions.",
                worker_id,
            )

        config = SnowflakeConfig(
            epoch=epoch,
            total_bits=total,
            time_bits=time_bits,
            node_bits=node_bits,
            node_id=node_id,
            worker_id=worker_id,
            worker_bits=worker_bits,
        )
        self.generator = SnowflakeGenerator(config=config)

    @property
    def id_type(self) -> IDType:
        return IDType.BIGINT

    async def generate(self) -> int:
        """Generate a new Snowflake ID.

        Returns:
            int: A new Snowflake ID.
        """
        return await self.generator.generate()
