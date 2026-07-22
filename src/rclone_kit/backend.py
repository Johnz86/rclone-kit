"""Internal command-execution boundary for rclone operations."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rclone_kit.config import Config
from rclone_kit.process import Process, ProcessArgs
from rclone_kit.util import rclone_execute


class RcloneBackend(Protocol):
    """Structural execution contract consumed by operation modules."""

    def run(
        self,
        command: tuple[str, ...],
        *,
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]: ...

    def launch(
        self,
        command: tuple[str, ...],
        *,
        capture: bool | None = None,
        log: Path | None = None,
    ) -> Process: ...


@dataclass(frozen=True)
class CliRcloneBackend:
    """Execute rclone commands through the bundled subprocess adapters."""

    rclone_config: Path | Config | None
    rclone_exe: Path

    def run(
        self,
        command: tuple[str, ...],
        *,
        check: bool = False,
        capture: bool | Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return rclone_execute(
            list(command),
            self.rclone_config,
            self.rclone_exe,
            check=check,
            capture=capture,
        )

    def launch(
        self,
        command: tuple[str, ...],
        *,
        capture: bool | None = None,
        log: Path | None = None,
    ) -> Process:
        args = ProcessArgs(
            rclone_conf=self.rclone_config,
            rclone_exe=self.rclone_exe,
            cmd_list=list(command),
            capture_stdout=capture,
            log=log,
        )
        return Process(args)
