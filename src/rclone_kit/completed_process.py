import subprocess
from dataclasses import dataclass, field

from rclone_kit.operation import OperationResult
from rclone_kit.util import format_command


@dataclass
class CompletedProcess:
    """Compatibility result type shared by the CLI and embedded backends.

    `completed`/`stdout`/`stderr`/`failed()`/`successes()`/command
    formatting are CLI-only and deprecated: they reflect real subprocess
    invocations and are never populated with invented data for an
    embedded-backed result. An embedded-backed instance (constructed via
    `from_operation_result()`) instead carries `operation_result`; `.ok`
    and `.returncode` delegate to it, and `completed` stays empty.

    Per the CLI-to-C-ABI migration's Wave D design (section 5.6), this is
    itself deprecated in favor of returning `OperationResult` directly, once
    the embedded-first major release removes the CLI backend.
    """

    completed: list[subprocess.CompletedProcess[str]] = field(default_factory=list)
    operation_result: OperationResult | None = None

    @property
    def ok(self) -> bool:
        if self.operation_result is not None:
            return self.operation_result.ok
        return all(p.returncode == 0 for p in self.completed)

    @staticmethod
    def from_subprocess(process: subprocess.CompletedProcess[str]) -> "CompletedProcess":
        return CompletedProcess(completed=[process])

    @staticmethod
    def from_operation_result(result: OperationResult) -> "CompletedProcess":
        return CompletedProcess(completed=[], operation_result=result)

    def failed(self) -> list[subprocess.CompletedProcess[str]]:
        return [p for p in self.completed if p.returncode != 0]

    def successes(self) -> list[subprocess.CompletedProcess[str]]:
        return [p for p in self.completed if p.returncode == 0]

    @property
    def stdout(self) -> str:
        tmp: list[str] = []
        for cp in self.completed:
            stdout = cp.stdout
            if stdout is not None:
                tmp.append(stdout)
        return "\n".join(tmp)

    @property
    def stderr(self) -> str:
        tmp: list[str] = []
        for cp in self.completed:
            stderr = cp.stderr
            if stderr is not None:
                tmp.append(stderr)
        return "\n".join(tmp)

    @property
    def returncode(self) -> int:
        if self.operation_result is not None:
            return 0 if self.operation_result.ok else 1
        for cp in self.completed:
            rtn = cp.returncode
            if rtn != 0:
                return rtn
        return 0

    def __str__(self) -> str:
        if self.operation_result is not None:
            return f"CompletedProcess: wraps {self.operation_result!r}"

        cmd_strs: list[str] = []
        rtn_cods: list[int] = []
        for cp in self.completed:
            cmd_strs.append(format_command(cp.args))
            rtn_cods.append(cp.returncode)
        msg = f"CompletedProcess: {len(cmd_strs)} commands\n"
        msg += "\n".join([f"{cmd} -> {rtn}" for cmd, rtn in zip(cmd_strs, rtn_cods, strict=False)])
        return msg
