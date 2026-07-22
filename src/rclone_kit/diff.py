import _thread
from collections.abc import Generator
from dataclasses import dataclass
from enum import Enum
from queue import Queue
from threading import Thread

from rclone_kit.process import Process


class DiffType(Enum):
    EQUAL = "="
    MISSING_ON_SRC = "-"
    MISSING_ON_DST = "+"
    DIFFERENT = "*"
    ERROR = "!"


class DiffOption(Enum):
    COMBINED = "combined"
    MISSING_ON_SRC = "missing-on-src"
    MISSING_ON_DST = "missing-on-dst"
    DIFFER = "differ"
    MATCH = "match"
    ERROR = "error"


_MISSING_ON_DIFF_TYPE = {
    DiffOption.MISSING_ON_SRC: DiffType.MISSING_ON_SRC,
    DiffOption.MISSING_ON_DST: DiffType.MISSING_ON_DST,
}


@dataclass
class DiffItem:
    type: DiffType
    path: str
    src_prefix: str
    dst_prefix: str

    def __str__(self) -> str:
        return f"{self.type.value} {self.path}"

    def __repr__(self) -> str:
        return f"{self.type.name} {self.path}"

    def full_str(self) -> str:
        return f"{self.type.name} {self.src_prefix}/{self.path} {self.dst_prefix}/{self.path}"

    def dst_path(self) -> str:
        return f"{self.dst_prefix}/{self.path}"

    def src_path(self) -> str:
        return f"{self.src_prefix}/{self.path}"


def _parse_missing_on_src_dst(line: str) -> str | None:
    """A `--missing-on-dst`/`--missing-on-src` report line is a bare
    relative path, one per line, with nothing else on it.

    This only works because `stream_diff` gives rclone's own logging a
    dedicated `--log-file` rather than letting it merge into stdout - the
    stream this function reads from carries only the report itself.
    """
    stripped = line.strip()
    return stripped or None


def _classify_diff(
    line: str, src_slug: str, dst_slug: str, diff_option: DiffOption
) -> DiffItem | None:
    def _new(type: DiffType, path: str) -> DiffItem:
        return DiffItem(type, path, src_prefix=src_slug, dst_prefix=dst_slug)

    if diff_option == DiffOption.COMBINED:
        suffix = line[1:].strip() if len(line) > 0 else ""
        if line.startswith(DiffType.EQUAL.value):
            return _new(DiffType.EQUAL, suffix)
        if line.startswith(DiffType.MISSING_ON_SRC.value):
            return _new(DiffType.MISSING_ON_SRC, suffix)
        if line.startswith(DiffType.MISSING_ON_DST.value):
            return _new(DiffType.MISSING_ON_DST, suffix)
        if line.startswith(DiffType.DIFFERENT.value):
            return _new(DiffType.DIFFERENT, suffix)
        if line.startswith(DiffType.ERROR.value):
            return _new(DiffType.ERROR, suffix)
        return None
    if diff_option in _MISSING_ON_DIFF_TYPE:
        filename = _parse_missing_on_src_dst(line)
        if filename is not None:
            return _new(_MISSING_ON_DIFF_TYPE[diff_option], filename)
        return None
    raise ValueError(f"Unknown diff_option: {diff_option}")


def _async_diff_stream_from_running_process(
    running_process: Process,
    src_slug: str,
    dst_slug: str,
    diff_option: DiffOption,
    output: Queue[DiffItem | None],
) -> None:
    count = 0
    first_few_lines: list[str] = []
    try:
        assert running_process.stdout is not None
        n_max = 10
        for line in iter(running_process.stdout.readline, b""):
            try:
                line_str = line.decode("utf-8").strip()
                if len(first_few_lines) < n_max:
                    first_few_lines.append(line_str)

                diff_item: DiffItem | None = _classify_diff(
                    line_str, src_slug, dst_slug, diff_option
                )
                if diff_item is None:
                    continue
                output.put(diff_item)
                count += 1

            except UnicodeDecodeError:
                print("UnicodeDecodeError")
                continue
    except KeyboardInterrupt:
        print("KeyboardInterrupt")
        _thread.interrupt_main()
    except Exception as e:
        print(f"Error: {e}")
        _thread.interrupt_main()
    finally:
        output.put(None)


def diff_stream_from_running_process(
    running_process: Process,
    src_slug: str,
    dst_slug: str,
    diff_option: DiffOption,
) -> Generator[DiffItem]:
    output: Queue[DiffItem | None] = Queue()

    def _task() -> None:
        _async_diff_stream_from_running_process(
            running_process, src_slug, dst_slug, diff_option, output
        )

    thread = Thread(target=_task, daemon=True)
    thread.start()
    while True:
        item = output.get()
        if item is None:
            break
        yield item
    thread.join(timeout=5)
