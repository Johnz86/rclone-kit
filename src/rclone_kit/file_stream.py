"""
Unit test file.
"""

from collections.abc import Generator
from typing import Self

from rclone_kit.file import FileItem
from rclone_kit.process import Process


class FilesStream:
    def __init__(self, path: str, process: Process) -> None:
        self.path = path
        self.process = process

    def __enter__(self) -> Self:
        self.process.__enter__()
        return self

    def __exit__(self, *exc_info):
        self.process.__exit__(*exc_info)

    def files(self) -> Generator[FileItem]:
        line: bytes
        for line in self.process.stdout:
            linestr: str = line.decode("utf-8").strip()
            if linestr.startswith("["):
                continue
            if linestr.endswith(","):
                linestr = linestr[:-1]
            if linestr.endswith("]"):
                continue
            fileitem: FileItem | None = FileItem.from_json_str(self.path, linestr)
            if fileitem is None:
                continue
            yield fileitem

    def files_paged(self, page_size: int = 1000) -> Generator[list[FileItem]]:
        page: list[FileItem] = []
        for fileitem in self.files():
            page.append(fileitem)
            if len(page) >= page_size:
                yield page
                page = []
        if len(page) > 0:
            yield page

    def __iter__(self) -> Generator[FileItem]:
        return self.files()
