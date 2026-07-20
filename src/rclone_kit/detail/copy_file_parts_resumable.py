from __future__ import annotations

from rclone_kit.s3.multipart.access import MultipartAccess
from rclone_kit.types import (
    PartInfo,
)
from rclone_kit.util import get_verbose


def copy_file_parts_resumable(
    access: MultipartAccess,
    src: str,
    dst_dir: str,
    part_infos: list[PartInfo] | None = None,
    upload_threads: int = 10,
    merge_threads: int = 5,
    verbose: bool | None = None,
) -> None:
    from rclone_kit.s3.multipart.upload_parts_resumable import upload_parts_resumable
    from rclone_kit.s3.multipart.upload_parts_server_side_merge import (
        s3_server_side_multi_part_merge,
    )

    if verbose is None:
        verbose = get_verbose(None)

    upload_parts_resumable(
        self=access,
        src=src,
        dst_dir=dst_dir,
        part_infos=part_infos,
        threads=upload_threads,
    )
    if dst_dir.endswith("/"):
        dst_dir = dst_dir[:-1]
    dst_info = f"{dst_dir}/info.json"
    s3_server_side_multi_part_merge(
        rclone=access,
        info_path=dst_info,
        max_workers=merge_threads,
        verbose=verbose,
    )
