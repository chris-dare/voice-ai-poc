from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config


async def upgrade_database() -> None:
    config = Config(str(Path(__file__).parent.parent.parent / "alembic.ini"))
    await asyncio.to_thread(command.upgrade, config, "head")
