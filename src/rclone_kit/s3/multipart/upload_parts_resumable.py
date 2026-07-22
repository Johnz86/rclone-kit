from __future__ import annotations

import _thread
import atexit
import logging
import os
import shutil
import threading
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from rclone_kit.exceptions import S3UploadError
from rclone_kit.http_server import HttpServer
from rclone_kit.s3.multipart.access import MultipartAccess
from rclone_kit.s3.multipart.info_json import InfoJson
from rclone_kit.types import (
    PartInfo,
    Range,
    SizeSuffix,
)
from rclone_kit.util import random_str

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

_TMP_UPLOAD_DIRS: set[Path] = set()
_MIN_PART_UPLOAD_SIZE = SizeSuffix("5MB")


def _cleanup_tmp_upload_dirs() -> None:
    """Remove every temporary chunk directory still tracked at exit.

    Registered once at import time rather than once per
    `upload_parts_resumable` call, so a long-running process performing
    many resumable uploads does not grow `atexit`'s internal registration
    list without bound. A tmp_dir stays tracked here until
    `upload_parts_resumable` itself removes it after a successful cleanup,
    so an upload that raises before reaching that point still gets a
    best-effort removal at process exit.
    """
    for tmp_dir in list(_TMP_UPLOAD_DIRS):
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _register_exit_cleanup_handlers() -> None:
    """Register this module's `atexit` handler, once, at import time.

    Wrapped in a named function rather than left as a bare
    `atexit.register(...)` statement, so this module's exit-time side
    effect is discoverable by name instead of blending into the
    surrounding statement flow. Unlike the lazy, first-use-guarded
    registration in `util.py`/`process.py`/`file_part.py`, "at import time"
    is correct here rather than a leftover eager pattern: this module is
    itself only ever imported function-locally at its one real call site,
    so module import and first real use already coincide.
    """
    atexit.register(_cleanup_tmp_upload_dirs)


_register_exit_cleanup_handlers()


def _append_upload_log(filename: str, msg: str) -> None:
    if os.getenv("LOG_UPLOAD_S3_RESUMABLE") == "1":
        log_path = Path("log") / filename
        with _LOCK:
            log_path.parent.mkdir(parents=True, exist_ok=True)

            with open(log_path, mode="a", encoding="utf-8") as f:
                f.write(msg)
                f.write("\n")


def _log(msg: str) -> None:
    logger.info(msg)
    _append_upload_log("s3_resumable_upload.log", msg)


def _log_completed_item(msg: str) -> None:
    _append_upload_log("s3_resumable_upload_completed.log", msg)


@dataclass
class UploadPart:
    chunk: Path
    dst_part: str
    part_num: int
    total_parts: int
    total_size: SizeSuffix
    exception: Exception | None = None
    finished: bool = False

    def dispose(self):
        try:
            if self.chunk.exists():
                self.chunk.unlink()
            self.finished = True
        except Exception as e:
            warnings.warn(f"Failed to delete file {self.chunk}: {e}", stacklevel=2)

    def __del__(self):
        self.dispose()


def _gen_name(part_number: int, offset: SizeSuffix, end: SizeSuffix) -> str:
    return f"part.{part_number:05d}_{offset.as_int()}-{end.as_int()}"


def upload_task(access: MultipartAccess, upload_part: UploadPart) -> UploadPart:
    try:
        if upload_part.exception is not None:
            return upload_part

        num_parts = upload_part.total_parts
        total_size = upload_part.total_size
        part_num = upload_part.part_num
        msg = "\n#############################################################\n"
        msg += f"# Uploading {upload_part.chunk} to {upload_part.dst_part}\n"
        msg += f"# Part number: {part_num} / {num_parts}\n"
        msg += f"# Total parts: {num_parts}\n"
        msg += f"# Total size: {total_size.as_int()} bytes\n"
        msg += f"# Chunk size: {upload_part.chunk.stat().st_size} bytes\n"
        msg += f"# Range: {upload_part.chunk.name}\n"
        msg += "##############################################################\n"
        _log(msg)
        access.copy_to(upload_part.chunk.as_posix(), upload_part.dst_part)
        return upload_part
    except Exception as e:
        upload_part.exception = e
        return upload_part
    finally:
        upload_part.dispose()


def read_task(
    http_server: HttpServer,
    src_name: str,
    tmpdir: Path,
    offset: SizeSuffix,
    length: SizeSuffix,
    part_dst: str,
    part_number: int,
    total_parts: int,
    total_size: SizeSuffix,
) -> UploadPart:
    outchunk: Path = tmpdir / f"{offset.as_int()}-{(offset + length).as_int()}.chunk"
    range = Range(offset.as_int(), (offset + length).as_int())

    try:
        http_server.download(
            path=src_name,
            range=range,
            dst=outchunk,
        )
        return UploadPart(
            chunk=outchunk,
            dst_part=part_dst,
            part_num=part_number,
            total_parts=total_parts,
            total_size=total_size,
        )
    except KeyboardInterrupt:
        _thread.interrupt_main()
        raise
    except SystemExit:
        _thread.interrupt_main()
        raise
    except Exception as e:
        return UploadPart(
            chunk=outchunk,
            dst_part=part_dst,
            part_num=part_number,
            total_parts=total_parts,
            total_size=total_size,
            exception=e,
        )


def collapse_runs(numbers: list[int]) -> list[str]:
    if not numbers:
        return []

    runs = []
    start = numbers[0]
    prev = numbers[0]

    for num in numbers[1:]:
        if num == prev + 1:
            prev = num
        else:
            if start == prev:
                runs.append(str(start))
            else:
                runs.append(f"{start}-{prev}")
            start = num
            prev = num

    if start == prev:
        runs.append(str(start))
    else:
        runs.append(f"{start}-{prev}")

    return runs


def _check_part_size(parts: list[PartInfo]) -> None:
    """Raises `ValueError` if `parts` is empty or its parts are too small
    to upload via server-side merge."""
    if len(parts) == 0:
        raise ValueError("No parts to upload")
    part = parts[0]
    chunk = part.range.end - part.range.start
    if chunk < _MIN_PART_UPLOAD_SIZE:
        raise ValueError(
            f"Part size {chunk} is too small to upload. Minimum size for server side merge is {_MIN_PART_UPLOAD_SIZE}"
        )


def upload_parts_resumable(
    self: MultipartAccess,
    src: str,
    dst_dir: str,
    part_infos: list[PartInfo] | None = None,
    threads: int = 1,
    verbose: bool | None = None,
) -> None:
    """Copy parts of a file from source to destination.

    Raises `ValueError` if `part_infos` is empty or its parts are too
    small, or `S3UploadError` if any part fails to upload.
    """

    def verbose_print(msg: str) -> None:
        if verbose:
            logger.info(msg)

    if dst_dir.endswith("/"):
        dst_dir = dst_dir[:-1]
    src_size = self.size_file(src)

    part_info: PartInfo
    src_dir = os.path.dirname(src)
    src_name = os.path.basename(src)
    http_server: HttpServer

    full_part_infos: list[PartInfo] = PartInfo.split_parts(src_size, SizeSuffix("96MB"))

    if part_infos is None:
        part_infos = full_part_infos.copy()

    _check_part_size(part_infos)

    all_part_numbers: list[int] = [p.part_number for p in part_infos]
    src_info_json = f"{dst_dir}/info.json"
    info_json = InfoJson(self, src, src_info_json)

    if not info_json.load():
        verbose_print(f"New: {src_info_json}")

    all_numbers_already_done: set[int] = set(info_json.fetch_all_finished_part_numbers())

    first_part_number = part_infos[0].part_number
    last_part_number = part_infos[-1].part_number

    verbose_print(f"all_numbers_already_done: {collapse_runs(sorted(all_numbers_already_done))}")

    total_parts = len(part_infos)
    part_infos = [
        part_info
        for part_info in part_infos
        if part_info.part_number not in all_numbers_already_done
    ]
    remaining_part_numbers: list[int] = [p.part_number for p in part_infos]
    verbose_print(f"remaining_part_numbers: {collapse_runs(remaining_part_numbers)}")
    num_remaining_to_upload = len(part_infos)
    verbose_print(f"num_remaining_to_upload: {num_remaining_to_upload} / {len(full_part_infos)}")

    if num_remaining_to_upload == 0:
        return
    chunk_size = SizeSuffix(part_infos[0].range.end - part_infos[0].range.start)

    info_json.chunksize = chunk_size

    info_json.first_part = first_part_number
    info_json.last_part = last_part_number
    info_json.save()

    info_json.load()
    logger.debug("%s", info_json)

    finished_tasks: list[UploadPart] = []
    tmp_dir = str(Path("chunks") / random_str(12))
    _TMP_UPLOAD_DIRS.add(Path(tmp_dir))

    with self.serve_http(src_dir) as http_server:
        tmpdir: Path = Path(tmp_dir)
        write_semaphore = threading.Semaphore(threads)
        with (
            ThreadPoolExecutor(max_workers=threads) as upload_executor,
            ThreadPoolExecutor(max_workers=threads) as read_executor,
        ):
            for part_info in part_infos:
                part_number: int = part_info.part_number
                range: Range = part_info.range
                offset: SizeSuffix = SizeSuffix(range.start)
                length: SizeSuffix = SizeSuffix(range.end - range.start)
                end = offset + length
                suffix = _gen_name(part_number, offset, end)
                part_dst = f"{dst_dir}/{suffix}"

                def _read_task(
                    src_name=src_name,
                    http_server=http_server,
                    tmpdir=tmpdir,
                    offset=offset,
                    length=length,
                    part_dst=part_dst,
                    part_number=part_number,
                ) -> UploadPart:
                    return read_task(
                        src_name=src_name,
                        http_server=http_server,
                        tmpdir=tmpdir,
                        offset=offset,
                        length=length,
                        part_dst=part_dst,
                        part_number=part_number,
                        total_parts=total_parts,
                        total_size=src_size,
                    )

                read_fut: Future[UploadPart] = read_executor.submit(_read_task)

                def queue_upload_task(
                    read_fut=read_fut,
                ) -> None:
                    upload_part = read_fut.result()
                    upload_fut: Future[UploadPart] = upload_executor.submit(
                        upload_task, self, upload_part
                    )

                    upload_fut.add_done_callback(lambda _: write_semaphore.release())
                    upload_fut.add_done_callback(lambda fut: finished_tasks.append(fut.result()))

                read_fut.add_done_callback(queue_upload_task)

                write_semaphore.acquire()

    exceptions: list[Exception] = [t.exception for t in finished_tasks if t.exception is not None]

    shutil.rmtree(tmp_dir, ignore_errors=True)
    _TMP_UPLOAD_DIRS.discard(Path(tmp_dir))

    if len(exceptions) > 0:
        msg = f"Failed to copy parts: {exceptions}"
        _log(msg)
        raise S3UploadError(exceptions)

    finished_parts: list[int] = info_json.fetch_all_finished_part_numbers()
    logger.info("finished_names: %s", finished_parts)

    diff_set = set(all_part_numbers).symmetric_difference(set(finished_parts))
    all_part_numbers_done = len(diff_set) == 0

    full_path = os.path.join(dst_dir, src_name)
    if all_part_numbers_done:
        msg = f"Upload completed: {full_path} ({len(finished_parts)}/{len(all_part_numbers)})"
        info_json.save()
    else:
        msg = f"Upload failed for {full_path} ({len(finished_parts)}/{len(all_part_numbers)})"
    _log(msg)
    _log_completed_item(msg)
