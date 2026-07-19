"""Database module for rclone_kit."""

import logging
import os
from threading import Lock
from typing import Self

from sqlmodel import Session, SQLModel, col, create_engine, select

from rclone_kit.db.models import RepositoryMeta, create_file_entry_model
from rclone_kit.file import FileItem

logger = logging.getLogger(__name__)


def _to_table_name(remote_name: str) -> str:
    return "files_" + remote_name.replace(":", "_").replace(" ", "_").replace("/", "_").lower()


class DB:
    """Database class for rclone_kit."""

    def __init__(self, db_path_url: str):
        """Initialize the database.

        Args:
            db_path: Path to the database file
        """
        self.db_path_url = db_path_url

        retries = 2
        for _ in range(retries):
            try:
                self.engine = create_engine(db_path_url)
                SQLModel.metadata.create_all(self.engine)
                break
            except Exception as e:
                logger.warning("Failed to connect to database. Retrying... %s", e)
        else:
            raise Exception("Failed to connect to database.")
        self._cache: dict[str, DBRepo] = {}
        self._cache_lock = Lock()

    def drop_all(self) -> None:
        """Drop all tables in the database."""
        SQLModel.metadata.drop_all(self.engine)

    def close(self) -> None:
        """Close the database connection and release resources."""
        if hasattr(self, "engine") and self.engine is not None:
            self.engine.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def add_files(self, files: list[FileItem]) -> None:
        """Add files to the database.

        Args:
            remote_name: Name of the remote
            files: List of file entries
        """

        partition: dict[str, list[FileItem]] = {}
        for file in files:
            partition.setdefault(file.remote, []).append(file)

        for remote_name, remote_files in partition.items():
            repo = self.get_or_create_repo(remote_name)
            repo.insert_files(remote_files)

    def query_all_files(self, remote_name: str) -> list[FileItem]:
        """Query files from the database.

        Args:
            remote_name: Name of the remote
        """
        repo = self.get_or_create_repo(remote_name)
        files = repo.get_all_files()
        return list(files)

    def get_or_create_repo(self, remote_name: str) -> "DBRepo":
        """Get a table section for a remote.

        Args:
            remote_name: Name of the remote
            table_name: Optional table name, will be derived from remote_name if not provided

        Returns:
            DBRepo: A table section for the remote
        """
        with self._cache_lock:
            if remote_name in self._cache:
                return self._cache[remote_name]
            table_name = _to_table_name(remote_name)
            out = DBRepo(self.engine, remote_name, table_name)
            self._cache[remote_name] = out
            return out


class DBRepo:
    """Table repo remote."""

    def __init__(self, engine, remote_name: str, table_name: str | None = None):
        """Initialize a table section.

        Args:
            engine: SQLAlchemy engine
            remote_name: Name of the remote
            table_name: Optional table name, will be derived from remote_name if not provided
        """
        self.engine = engine
        self.remote_name = remote_name

        if table_name is None:
            table_name = _to_table_name(remote_name)
        self.table_name = table_name

        with Session(self.engine) as session:
            existing_repo = session.exec(
                select(RepositoryMeta).where(RepositoryMeta.repo_name == self.remote_name)
            ).first()
            if not existing_repo:
                repo_meta = RepositoryMeta(
                    repo_name=self.remote_name, file_table_name=self.table_name
                )
                session.add(repo_meta)
                session.commit()

        self.FileEntryModel = create_file_entry_model(self.table_name)
        SQLModel.metadata.create_all(
            self.engine,
            tables=[SQLModel.metadata.tables[self.table_name]],
        )

    def insert_file(self, file: FileItem) -> None:
        """Insert a file entry into the table.

        Args:
            file: File entry
        """
        return self.insert_files([file])

    def insert_files(self, files: list[FileItem]) -> None:
        """
        Insert multiple file entries into the table.

        Three bulk operations are performed:
        1. Select: Determine which files already exist.
        2. Insert: Bulk-insert new file entries.
        3. Update: Bulk-update existing file entries.

        The FileEntryModel must define a unique constraint on (path, name) and have a primary key "id".
        """

        existing_files = self.get_exists(files)

        needs_update = existing_files
        is_new = set(files) - existing_files

        new_values = [
            {
                "path": file.path_no_remote,
                "name": file.name,
                "size": file.size,
                "mime_type": file.mime_type,
                "mod_time": file.mod_time,
                "suffix": file.real_suffix,
            }
            for file in is_new
        ]
        with Session(self.engine) as session:
            if new_values:
                session.bulk_insert_mappings(self.FileEntryModel, new_values)
                session.commit()

        with Session(self.engine) as session:
            update_paths = [file.path_no_remote for file in needs_update]

            db_entries = session.exec(
                select(self.FileEntryModel).where(col(self.FileEntryModel.path).in_(update_paths))
            ).all()

            id_map = {(entry.path, entry.name): entry.id for entry in db_entries}

            update_values = []
            for file in needs_update:
                key = (file.path_no_remote, file.name)
                if key in id_map:
                    update_values.append(
                        {
                            "id": id_map[key],
                            "size": file.size,
                            "mime_type": file.mime_type,
                            "mod_time": file.mod_time,
                            "suffix": file.real_suffix,
                        }
                    )
            if update_values:
                session.bulk_update_mappings(self.FileEntryModel, update_values)
                session.commit()

    def get_exists(self, files: list[FileItem]) -> set[FileItem]:
        """Get file entries from the table that exist among the given files.

        Args:
            files: List of file entries

        Returns:
            Set of FileItem instances whose 'path_no_remote' exists in the table.
        """

        paths = {file.path_no_remote for file in files}

        with Session(self.engine) as session:
            result = session.exec(
                select(self.FileEntryModel.path).where(col(self.FileEntryModel.path).in_(paths))
            ).all()

            existing_paths = set(result)

        return {file for file in files if file.path_no_remote in existing_paths}

    def get_all_files(self) -> list[FileItem]:
        """Get all files in the table.

        Returns:
            list: List of file entries
        """

        out: list[FileItem] = []
        with Session(self.engine) as session:
            query = session.exec(select(self.FileEntryModel)).all()
            for item in query:
                name = item.name
                size = item.size
                mime_type = item.mime_type
                mod_time = item.mod_time
                path = item.path
                parent = os.path.dirname(path)
                if parent in {"/", "."}:
                    parent = ""
                o = FileItem(
                    remote=self.remote_name,
                    parent=parent,
                    name=name,
                    size=size,
                    mime_type=mime_type,
                    mod_time=mod_time,
                )
                out.append(o)
        return out
