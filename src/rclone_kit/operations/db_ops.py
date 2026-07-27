"""Persisting a listing stream into a database.

Lives outside `rclone_kit.db` on purpose: importing that module is exactly
what fails when the optional `database` extra is absent, so the guard that
turns the resulting `ModuleNotFoundError` into a
`MissingOptionalDependencyError` cannot live inside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rclone_kit.optional_dependency import MissingOptionalDependencyError

if TYPE_CHECKING:
    from rclone_kit.embedded_file_stream import EmbeddedFilesStream

_DB_PAGE_SIZE = 10000
"""Rows accumulated per `add_files` call. Large enough that a
multi-million-file listing is not dominated by per-batch database
overhead, while still bounding how many `FileItem`s are held at once."""

_DB_FEATURE_NAME = "Database operations"
_DB_EXTRA_NAME = "database"
_DB_PACKAGE_NAME = "sqlmodel"


def save_stream_to_db(stream: EmbeddedFilesStream, db_url: str) -> None:
    """Drain `stream` into the database at `db_url`, one page at a time.

    Takes an already-open stream rather than opening one itself: the
    caller owns the stream's lifetime (it is a tracked client resource),
    and keeping the acquisition there leaves this function with no
    dependency on how a listing is started.
    """
    try:
        from rclone_kit.db import DB
    except ModuleNotFoundError as error:
        raise MissingOptionalDependencyError(
            _DB_FEATURE_NAME, _DB_EXTRA_NAME, _DB_PACKAGE_NAME
        ) from error

    db = DB(db_url)
    for page in stream.files_paged(page_size=_DB_PAGE_SIZE):
        db.add_files(page)
