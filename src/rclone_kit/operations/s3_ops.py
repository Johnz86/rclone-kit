"""S3-native operations that bypass rclone's own transfer path.

These are the entry points that talk to S3 through `boto3` instead of
through the embedded runtime, because the operation has no faithful RC
equivalent: a direct `PutObject` for a local file, and the resumable
part-by-part upload plus server-side merge.

`boto3` is an optional extra, so `rclone_kit.s3.api` is imported inside
`make_s3_client` rather than at module scope - importing this module
(and therefore `rclone_kit`) must keep working without the `s3` extra
installed, and the failure must be a `MissingOptionalDependencyError`
raised from the call that actually needs boto3.

Every function takes the calling client as `access` and reaches back
through its public methods (`is_s3()`, `get_s3_credentials()`,
`copy_to()`, ...), so credential resolution and remote-type detection
keep their single implementation on the client's own config snapshot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from rclone_kit.operations.copy_file_parts_resumable import copy_file_parts_resumable
from rclone_kit.optional_dependency import MissingOptionalDependencyError
from rclone_kit.s3.types import S3UploadTarget
from rclone_kit.types import S3PathInfo
from rclone_kit.util import get_verbose

if TYPE_CHECKING:
    from pathlib import Path

    from rclone_kit.s3.api import S3Client
    from rclone_kit.s3.multipart.access import MultipartAccess
    from rclone_kit.s3.types import S3Credentials
    from rclone_kit.types import PartInfo

_PARTS_DIR_SUFFIX = "-parts"


class S3CredentialsAccess(Protocol):
    """The credential lookup every boto3-backed operation needs."""

    def get_s3_credentials(self, remote: str, verbose: bool | None = None) -> S3Credentials: ...


class S3UploadAccess(S3CredentialsAccess, Protocol):
    """`S3CredentialsAccess` plus the remote-type check `upload_file_s3`
    refuses to upload without."""

    def is_s3(self, dst: str) -> bool: ...


def make_s3_client(access: S3CredentialsAccess, src: str, verbose: bool | None = None) -> S3Client:
    """Build a boto3-backed `S3Client` for the remote `src` names.

    Raises `MissingOptionalDependencyError` when the `s3` extra is not
    installed.
    """
    try:
        from rclone_kit.s3.api import S3Client
    except ModuleNotFoundError as error:
        raise MissingOptionalDependencyError("S3 operations", "s3", "boto3") from error

    verbose = get_verbose(verbose)
    s3_creds = access.get_s3_credentials(remote=src, verbose=verbose)
    return S3Client(s3_creds=s3_creds, verbose=verbose)


def upload_file_s3(
    access: S3UploadAccess, src: Path, dst: str, verbose: bool | None = None
) -> None:
    """Upload the local file `src` to the S3 destination `dst`.

    Raises `ValueError` if `dst` is not an S3 remote - the boto3 path
    cannot fall back to a generic rclone transfer, so a mistyped
    destination is rejected before any credential lookup.
    """
    if not access.is_s3(dst):
        raise ValueError(f"Destination is not an S3 remote: {dst}")
    s3_client = make_s3_client(access, dst, verbose=verbose)

    path_info: S3PathInfo = S3PathInfo.from_str(dst)
    target: S3UploadTarget = S3UploadTarget(
        src_file=src,
        src_file_size=src.stat().st_size,
        bucket_name=path_info.bucket,
        s3_key=path_info.key,
    )
    s3_client.upload_file(target=target)


def copy_file_parts_s3_resumable(
    access: MultipartAccess,
    src: str,
    dst: str,
    part_infos: list[PartInfo] | None = None,
    upload_threads: int = 8,
    merge_threads: int = 4,
) -> None:
    """Upload `src` to `dst` part by part, then merge the parts
    server-side.

    The parts are staged in a sibling `<dst>-parts` directory, so `dst`
    is normalized to a bare object path first: a trailing slash would
    otherwise produce a `<dst>/-parts` directory that the merge step,
    which reads `<parts dir>/info.json`, never looks in.
    """
    dst = dst.removesuffix("/")
    copy_file_parts_resumable(
        access=access,
        src=src,
        dst_dir=f"{dst}{_PARTS_DIR_SUFFIX}",
        part_infos=part_infos,
        upload_threads=upload_threads,
        merge_threads=merge_threads,
    )
