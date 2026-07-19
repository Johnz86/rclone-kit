"""
Unit test file for testing rclone mount functionality.
"""

import atexit
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

import pytest

from rclone_kit import Config, Rclone
from rclone_kit.env_file import load_env_file
from rclone_kit.exceptions import HttpFetchError
from rclone_kit.http_server import HttpServer, Range

load_env_file()

_CLEANUP: list[Path] = []


def _cleanup() -> None:
    for p in _CLEANUP:
        if p.exists():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()


atexit.register(_cleanup)


def hash_bytes(fp: Path) -> str:
    import hashlib

    sha256 = hashlib.sha256()
    with open(fp, "rb") as f:
        while chunk := f.read(4096):
            sha256.update(chunk)
    return sha256.hexdigest()


@pytest.mark.cloud
class RcloneServeHttpTester(unittest.TestCase):
    """Test rclone mount functionality."""

    @pytest.fixture(autouse=True)
    def _inject_do_spaces_config(self, do_spaces_config: Config) -> None:
        self.config = do_spaces_config

    def setUp(self) -> None:
        self.bucket_name = os.getenv("BUCKET_NAME")
        self.mount_point = Path("test_tmp_serve_http")
        _CLEANUP.append(self.mount_point)
        parent = self.mount_point.parent
        if not parent.exists():
            parent.mkdir(parents=True)

        os.environ["RCLONE_KIT_VERBOSE"] = "1"
        self.rclone = Rclone(self.config)

    def test_exists(self) -> None:
        """Test mounting a remote bucket."""
        remote_path = f"dst:{self.bucket_name}"
        http_server: HttpServer
        try:
            with self.rclone.serve_http(
                remote_path,
            ) as http_server:
                resource_url = "zachs_video/internaly_ai_alignment.mp4"
                exists = http_server.exists(resource_url)
                self.assertTrue(exists)

        except subprocess.CalledProcessError as e:
            self.fail(f"Mount operation failed: {e!s}")
        finally:
            pass

    def test_list(self) -> None:
        """Test mounting a remote bucket."""
        remote_path = f"dst:{self.bucket_name}"
        http_server: HttpServer
        try:
            with self.rclone.serve_http(
                remote_path,
            ) as http_server:
                resource_url = "zachs_video"
                try:
                    http_server.list(resource_url)
                except HttpFetchError as e:
                    self.fail(f"List operation failed: {e!s}")

        except subprocess.CalledProcessError as e:
            self.fail(f"Mount operation failed: {e!s}")
        finally:
            pass

    @unittest.skip("Skip for now")
    def test_server_http(self) -> None:
        """Test mounting a remote bucket."""
        remote_path = f"dst:{self.bucket_name}"
        http_server: HttpServer | None = None
        try:
            with self.rclone.serve_http(
                remote_path,
            ) as http_server:
                resource_url = "zachs_video/internaly_ai_alignment.mp4"
                expected_size = 73936110

                actual_size = http_server.size(resource_url)
                print(f"Actual size: {actual_size}")

                self.assertEqual(actual_size, expected_size)
                dst1 = self.mount_point / Path("zachs_video/internaly_ai_alignment.mp4.1")
                dst2 = self.mount_point / Path("zachs_video/internaly_ai_alignment.mp4.2")

                _CLEANUP.extend([Path("zachs_video"), dst1, dst2])

                start = time.time()
                out1 = http_server.download(resource_url, dst1)
                print(f"(1) Time taken: {time.time() - start}")
                start = time.time()
                out2 = http_server.download_multi_threaded(resource_url, dst2)
                print(f"(2) Time taken: {time.time() - start}")

                s1 = dst1.stat().st_size
                s2 = dst2.stat().st_size

                print(f"Size of {dst1}: {dst1.stat().st_size}")
                print(f"Size of {dst2}: {dst2.stat().st_size}")

                if s1 != s2:
                    with open(dst1, "rb") as f1, open(dst2, "rb") as f2:
                        bad_index = 0
                        while (chunk1 := f1.read(1)) and (chunk2 := f2.read(1)):
                            if chunk1 != chunk2:
                                break
                            bad_index += 1
                        print("bad index: ", bad_index)

                self.assertIsInstance(out1, Path)
                self.assertIsInstance(out2, Path)

                print(f"Bytes written: {out2.stat().st_size}")

                hash1 = hash_bytes(dst1)
                hash2 = hash_bytes(dst2)

                print(dst1.absolute())
                print(dst2.absolute())

                self.assertEqual(hash1, hash2)
                print("Done")

        except subprocess.CalledProcessError as e:
            self.fail(f"Mount operation failed: {e!s}")
        finally:
            pass

    @unittest.skip("Skip for now")
    def test_small_range(self) -> None:
        """Test mounting a remote bucket."""
        remote_path = f"dst:{self.bucket_name}"
        http_server: HttpServer | None = None
        try:
            with self.rclone.serve_http(src=remote_path, addr="localhost:8082") as http_server:
                resource_url = "zachs_video/internaly_ai_alignment.mp4"
                expected_size = 73936110

                actual_size = http_server.size(resource_url)
                print(f"Actual size: {actual_size}")

                self.assertEqual(actual_size, expected_size)
                dst1 = self.mount_point / Path("zachs_video/internaly_ai_alignment.mp4-2.1")
                dst2 = self.mount_point / Path("zachs_video/internaly_ai_alignment.mp4-2.2")

                _CLEANUP.extend([Path("zachs_video"), dst1, dst2])
                range: Range = Range(0, 1000)

                start = time.time()
                out1 = http_server.download(resource_url, dst1, range=range)
                print(f"(1) Time taken: {time.time() - start}")
                start = time.time()
                out2 = http_server.download_multi_threaded(resource_url, dst2, range=range)
                print(f"(2) Time taken: {time.time() - start}")

                s1 = dst1.stat().st_size
                s2 = dst2.stat().st_size

                print(f"Size of {dst1}: {dst1.stat().st_size}")
                print(f"Size of {dst2}: {dst2.stat().st_size}")

                if s1 != s2:
                    with open(dst1, "rb") as f1, open(dst2, "rb") as f2:
                        bad_index = 0
                        while (chunk1 := f1.read(1)) and (chunk2 := f2.read(1)):
                            if chunk1 != chunk2:
                                break
                            bad_index += 1
                        print("bad index: ", bad_index)

                self.assertIsInstance(out1, Path)
                self.assertIsInstance(out2, Path)

                print(f"Bytes written: {out2.stat().st_size}")

                def hash_bytes(fp: Path) -> str:
                    import hashlib

                    sha256 = hashlib.sha256()
                    with open(fp, "rb") as f:
                        while chunk := f.read(4096):
                            sha256.update(chunk)
                    return sha256.hexdigest()

                hash1 = hash_bytes(dst1)
                hash2 = hash_bytes(dst2)

                print(dst1.absolute())
                print(dst2.absolute())

                self.assertEqual(hash1, hash2)
                print("Done")

        except subprocess.CalledProcessError as e:
            self.fail(f"Mount operation failed: {e!s}")
        finally:
            pass


if __name__ == "__main__":
    unittest.main()
