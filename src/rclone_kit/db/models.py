"""
Database models for rclone_kit.
"""

from abc import ABC, abstractmethod

from sqlalchemy import BigInteger, Column
from sqlmodel import Field, SQLModel


# Meta table that indexes all repositories
class RepositoryMeta(SQLModel, table=True):
    """Repository metadata table."""

    id: int | None = Field(default=None, primary_key=True)
    repo_name: str
    file_table_name: str  # The dedicated table name for file entries


# Base FileEntry model that will be extended
class FileEntry(SQLModel, ABC):
    """Base file entry model with common fields."""

    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)
    suffix: str = Field(index=True)
    name: str
    size: int = Field(sa_column=Column(BigInteger))
    mime_type: str
    mod_time: str
    hash: str | None = Field(default=None)

    @abstractmethod
    def table_name(self) -> str:
        """Return the table name for this file entry model."""


# Factory to dynamically create a FileEntry model with a given table name
def create_file_entry_model(_table_name: str) -> type[FileEntry]:
    """Create a file entry model with a given table name.

    Args:
        table_name: Table name

    Returns:
        Type[FileEntryBase]: File entry model class with specified table name
    """

    class FileEntryConcrete(FileEntry, table=True):
        __tablename__ = _table_name  # type: ignore # dynamically set table name

        def table_name(self) -> str:
            return _table_name

    return FileEntryConcrete
