from __future__ import annotations

from typing import ClassVar

from sqlalchemy import MetaData
from sqlmodel import SQLModel


class TableModel(SQLModel):
    """Project-owned SQLModel registry, isolated from third-party table metadata."""

    metadata: ClassVar[MetaData] = MetaData()
