"""Unit tests for `rclone_kit.rc.fs_spec.encode_fs_spec`."""

from pathlib import Path

from rclone_kit.config import Config
from rclone_kit.rc.fs_spec import encode_fs_spec


def _abs(path: str) -> str:
    return str(Path(path).resolve())


_CONFIG_TEXT = """
[do-remote]
type = s3
provider = DigitalOcean
access_key_id = AKIAEXAMPLE
secret_access_key = super-secret
endpoint = s3.amazonaws.com

[b2-remote]
type = b2
account = accountid
key = appkey

[local-remote]
type = local

[gdrive-remote]
type = drive
"""


def _config() -> Config:
    return Config(_CONFIG_TEXT)


def test_s3_remote_becomes_a_config_object_with_no_check_bucket() -> None:
    encoded = encode_fs_spec(_config(), "do-remote:bucket/prefix")

    assert encoded == {
        "_name": "do-remote",
        "_root": "bucket/prefix",
        "no_check_bucket": "true",
    }


def test_b2_remote_also_becomes_a_config_object() -> None:
    encoded = encode_fs_spec(_config(), "b2-remote:bucket/prefix")

    assert encoded == {
        "_name": "b2-remote",
        "_root": "bucket/prefix",
        "no_check_bucket": "true",
    }


def test_s3_remote_root_with_no_further_path_still_encodes() -> None:
    encoded = encode_fs_spec(_config(), "do-remote:")

    assert encoded == {"_name": "do-remote", "_root": "", "no_check_bucket": "true"}


def test_non_s3_configured_remote_stays_a_plain_string() -> None:
    assert (
        encode_fs_spec(_config(), "gdrive-remote:folder/file.txt")
        == "gdrive-remote:folder/file.txt"
    )


def test_local_backed_remote_stays_a_plain_string() -> None:
    assert encode_fs_spec(_config(), "local-remote:some/path") == "local-remote:some/path"


def test_unknown_remote_name_stays_a_plain_string() -> None:
    assert encode_fs_spec(_config(), "not-configured:bucket/key") == "not-configured:bucket/key"


def test_local_windows_path_stays_a_plain_string() -> None:
    assert (
        encode_fs_spec(_config(), "C:\\Users\\example\\file.txt") == "C:\\Users\\example\\file.txt"
    )


def test_local_posix_path_gets_absolutized() -> None:
    # Regression: rclone's Fs cache would otherwise reuse the first
    # resolution of a relative/POSIX-style local reference for every later
    # call in this runtime's lifetime, regardless of cwd changes since -
    # see rc/paths.py's `_resolve_local`.
    assert encode_fs_spec(_config(), "/home/user/file.txt") == _abs("/home/user/file.txt")


def test_local_relative_path_gets_absolutized() -> None:
    assert encode_fs_spec(_config(), "relative/local/path.txt") == _abs("relative/local/path.txt")


def test_inline_remote_stays_a_plain_string_even_if_its_type_is_s3() -> None:
    # Inline remotes are anonymous - there is no config section to look up,
    # so this encoder deliberately leaves them untouched.
    spec = ":s3,provider=AWS:mybucket/prefix"
    assert encode_fs_spec(_config(), spec) == spec


def test_empty_config_never_raises() -> None:
    assert encode_fs_spec(Config(""), "do-remote:bucket/key") == "do-remote:bucket/key"
