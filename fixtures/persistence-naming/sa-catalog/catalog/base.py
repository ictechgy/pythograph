"""SQLAlchemy 2.x Declarative 기반들."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, declarative_base


class Base(DeclarativeBase):
    """기본 기반."""


LegacyBase = declarative_base(metadata=MetaData(schema="reporting"))
