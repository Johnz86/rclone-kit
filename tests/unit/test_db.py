"""
UUnit test file for the DB class.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rclone_kit import FileItem as DBFile
from rclone_kit.db import DB

HERE = Path(__file__).parent
DB_PATH = HERE / "test.db"

os.environ["DB_PATH"] = str(DB_PATH)


class RcloneDBTests(unittest.TestCase):
    """Test DB functionality."""

    def setUp(self) -> None:
        """Set up the test."""
        sql_url = "sqlite:///" + str(DB_PATH)
        self.db = DB(sql_url)

    def tearDown(self) -> None:
        """Clean up after the test."""

        self.db.close()
        if DB_PATH.exists():
            DB_PATH.unlink()

    def test_db_creation(self) -> None:
        """Test database creation."""
        self.assertTrue(DB_PATH.exists())

    def test_context_manager_closes_engine_on_exit(self) -> None:
        """`with DB(...)` calls close() on exit, same as calling it explicitly."""
        context_db_path = HERE / "test_context_manager.db"
        sql_url = "sqlite:///" + str(context_db_path)
        db = DB(sql_url)
        try:
            with patch.object(db, "close", wraps=db.close) as close_spy:
                with db:
                    pass
                close_spy.assert_called_once()
        finally:
            db.close()
            if context_db_path.exists():
                context_db_path.unlink()

    def test_table(self) -> None:
        """Test table section functionality."""

        repo = self.db.get_or_create_repo("dst:TorrentBooks")

        new_files = [
            DBFile(
                remote="dst:TorrentBooks",
                parent="",
                name="book1.pdf",
                size=2048,
                mime_type="application/pdf",
                mod_time="2025-03-03T12:00:00",
            ),
            DBFile(
                remote="dst:TorrentBooks",
                parent="",
                name="book2.epub",
                size=1024,
                mime_type="application/epub+zip",
                mod_time="2025-03-03T12:05:00",
            ),
        ]

        repo.insert_files(new_files)

        repo.insert_files(new_files)

        out_file_entries: list[DBFile] = repo.get_all_files()

        self.assertEqual(
            len(out_file_entries),
            2,
            f"Expected 2 file entries, found {len(out_file_entries)}",
        )

        for entry in out_file_entries:
            print(entry)
            self.assertIn(entry, new_files, f"Unexpected entry: {entry}")

    def test_init_disposes_a_failed_engine_before_retrying(self) -> None:
        """Regression test: __init__'s retry loop used to call
        create_engine() again on retry without disposing the previous
        failed attempt's engine, leaking it.
        """
        first_engine = MagicMock(name="first_engine")
        second_engine = MagicMock(name="second_engine")
        engines = iter([first_engine, second_engine])

        call_count = 0

        def fake_create_all(*_args, **_kwargs) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated connection failure")

        with (
            patch("rclone_kit.db.db.create_engine", side_effect=lambda *_a, **_k: next(engines)),
            patch("rclone_kit.db.db.SQLModel.metadata.create_all", side_effect=fake_create_all),
        ):
            db = DB("sqlite:///:memory:")

        first_engine.dispose.assert_called_once()
        second_engine.dispose.assert_not_called()
        self.assertIs(db.engine, second_engine)


if __name__ == "__main__":
    unittest.main()
