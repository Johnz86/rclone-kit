"""
Unit test file.
"""

import unittest

from rclone_kit.group_files import group_files as _group_files
from rclone_kit.group_files import group_under_one_prefix, group_under_remote_bucket


def group_files(files: list[str], fully_qualified: bool | None = None) -> dict[str, list[str]]:
    if fully_qualified is None:
        fully_qualified = False
    return _group_files(files, fully_qualified=fully_qualified)


class GroupFilestest(unittest.TestCase):
    """Test rclone functionality."""

    def test_simple_group_files(self) -> None:
        files = [
            "Bucket/subdir/file1.txt",
            "Bucket/subdir/file2.txt",
        ]
        groups: dict[str, list[str]] = group_files(files)
        self.assertEqual(len(groups), 1)

        self.assertIn("Bucket/subdir", groups)
        self.assertEqual(len(groups["Bucket/subdir"]), 2)
        expected_files = [
            "file1.txt",
            "file2.txt",
        ]
        self.assertIn(expected_files[0], groups["Bucket/subdir"])
        self.assertIn(expected_files[1], groups["Bucket/subdir"])
        print("done")

    def test_no_prefix(self) -> None:
        files = [
            "file1.txt",
            "file2.txt",
        ]
        groups: dict[str, list[str]] = group_files(files)
        self.assertEqual(len(groups), 1)

        self.assertIn("", groups)
        self.assertEqual(len(groups[""]), 2)
        expected_files = [
            "file1.txt",
            "file2.txt",
        ]
        self.assertIn(expected_files[0], groups[""])
        self.assertIn(expected_files[1], groups[""])
        print("done")

    def test_different_paths(self) -> None:
        files = [
            "Bucket/subdir/file1.txt",
            "Bucket/subdir2/file2.txt",
        ]
        groups: dict[str, list[str]] = group_files(files)
        self.assertEqual(len(groups), 2)

        self.assertIn("Bucket/subdir", groups)
        self.assertEqual(len(groups["Bucket/subdir"]), 1)
        expected_files = [
            "file1.txt",
        ]
        self.assertIn(expected_files[0], groups["Bucket/subdir"])

        self.assertIn("Bucket/subdir2", groups)
        self.assertEqual(len(groups["Bucket/subdir2"]), 1)

    def test_two_big_directories(self) -> None:
        files = [
            "Bucket/subdir/file1.txt",
            "Bucket/subdir/file2.txt",
            "Bucket/subdir2/file3.txt",
            "Bucket/subdir2/file4.txt",
        ]

        groups: dict[str, list[str]] = group_files(files)
        self.assertEqual(len(groups), 2)

        self.assertIn("Bucket/subdir", groups)
        self.assertEqual(len(groups["Bucket/subdir"]), 2)
        expected_files = [
            "file1.txt",
            "file2.txt",
        ]
        self.assertIn(expected_files[0], groups["Bucket/subdir"])
        self.assertIn(expected_files[1], groups["Bucket/subdir"])

        self.assertIn("Bucket/subdir2", groups)
        self.assertEqual(len(groups["Bucket/subdir2"]), 2)
        expected_files = [
            "file3.txt",
            "file4.txt",
        ]
        self.assertIn(expected_files[0], groups["Bucket/subdir2"])
        self.assertIn(expected_files[1], groups["Bucket/subdir2"])
        print("done")

    def test_two_fine_grained(self) -> None:
        files = [
            "TorrentBooks/libgenrs_nonfiction/204000/a2b20b2c89240ce81dec16091e18113e",
            "TorrentBooks/libgenrs_nonfiction/208000/155fe185bc03048b003a8e145ed097c8",
            "TorrentBooks/libgenrs_nonfiction/208001/155fe185bc03048b003a8e145ed097c8",
            "TorrentBooks/libgenrs_nonfiction/208002/155fe185bc03048b003a8e145ed097c8",
            "TorrentBooks/libgenrs_nonfiction/2080054/155fe185bc03048b003a8e145ed097c4",
        ]

        groups: dict[str, list[str]] = group_files(files)
        self.assertEqual(len(groups), 1)

        self.assertIn("TorrentBooks/libgenrs_nonfiction", groups)
        self.assertEqual(len(groups["TorrentBooks/libgenrs_nonfiction"]), 5)
        expected_files = [
            "204000/a2b20b2c89240ce81dec16091e18113e",
            "208000/155fe185bc03048b003a8e145ed097c8",
        ]
        self.assertIn(expected_files[0], groups["TorrentBooks/libgenrs_nonfiction"])

    def test_fully_qualified(self) -> None:
        files = [
            "dst:TorrentBooks/libgenrs_nonfiction/204000/a2b20b2c89240ce81dec16091e18113e",
        ]

        groups: dict[str, list[str]] = group_files(files, fully_qualified=True)
        self.assertEqual(len(groups), 1)

        self.assertIn("dst:TorrentBooks/libgenrs_nonfiction/204000", groups)
        self.assertEqual(len(groups["dst:TorrentBooks/libgenrs_nonfiction/204000"]), 1)
        expected_files = [
            "a2b20b2c89240ce81dec16091e18113e",
        ]
        self.assertIn(expected_files[0], groups["dst:TorrentBooks/libgenrs_nonfiction/204000"])

    def test_fully_qualified_local_path_has_no_colon(self) -> None:
        """`delete_files_embedded` calls `group_files(files)` (fully_qualified
        defaults to True) on real local filesystem paths too, via
        `RemoteFS.remove()` - on Linux those never contain a colon at all.
        A colonless path must group under its own absolute directory, not
        raise or get a stray leading ':' - see `parse_file`/
        `_fixup_rclone_paths`."""
        files = [
            "/srv/data/subdir/manifest.txt",
        ]

        groups: dict[str, list[str]] = _group_files(files, fully_qualified=True)

        self.assertEqual(groups, {"/srv/data/subdir": ["manifest.txt"]})

    def test_fully_qualified_windows_local_path_splits_on_backslash(self) -> None:
        """A Windows local path's own directory separators are backslashes,
        not "/" - `parse_file`/`group_files` must decompose parents using
        `RcPath.parse_parts` instead of assuming rclone's forward-slash
        remote-path convention, and reassemble the drive letter's colon
        with its separator kept (`C:/Users`, not the drive-relative
        `C:Users`) - see `_colonify`."""
        files = [
            r"C:\Users\jan\data\subdir\manifest.txt",
        ]

        groups: dict[str, list[str]] = _group_files(files, fully_qualified=True)

        self.assertEqual(groups, {"C:/Users/jan/data/subdir": ["manifest.txt"]})

    def test_group_under_remote_bucket_splits_by_bucket(self) -> None:
        files = [
            "dst:Bucket/subdir/file1.txt",
            "dst:Bucket/subdir2/file2.txt",
        ]

        groups = group_under_remote_bucket(files)

        self.assertEqual(
            groups,
            {
                "dst:Bucket": [
                    "subdir/file1.txt",
                    "subdir2/file2.txt",
                ]
            },
        )

    def test_group_under_remote_bucket_rejects_fully_qualified_false(self) -> None:
        with self.assertRaises(NotImplementedError):
            group_under_remote_bucket(["file1.txt"], fully_qualified=False)

    def test_group_under_one_prefix(self) -> None:
        files = [
            "Bucket/subdir/file1.txt",
            "Bucket/subdir/file2.txt",
        ]
        prefix, grouped_files = group_under_one_prefix("src:", files)
        self.assertEqual(prefix, "src:Bucket/subdir")
        self.assertEqual(len(grouped_files), 2)
        expected_files = [
            "file1.txt",
            "file2.txt",
        ]
        self.assertIn(expected_files[0], grouped_files)
        self.assertIn(expected_files[1], grouped_files)
        print("done")

    def test_group_under_one_prefix_preserves_literal_backslash_in_filename(self) -> None:
        """A filename containing a literal backslash (valid on S3 and most
        rclone backends) must not be split into two components. On Windows
        this used to fail because `_get_prefix` parsed the path with
        `pathlib.Path`, which resolves to `WindowsPath` there and treats
        `\\` as a directory separator; `PurePosixPath` never does.
        """
        backslash_name = "weird" + chr(92) + "name.txt"
        files = [f"Bucket/subdir/{backslash_name}"]

        prefix, grouped_files = group_under_one_prefix("src:", files)

        self.assertEqual(prefix, "src:Bucket/subdir")
        self.assertEqual(grouped_files, [backslash_name])


if __name__ == "__main__":
    unittest.main()
