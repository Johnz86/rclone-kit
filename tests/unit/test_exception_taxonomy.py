"""The library-wide exception root, and its reachability from callers.

`docs/production_usage.md` teaches `except RcloneKitError` as the boundary
handler that decides retry-or-alert policy. That advice was false for the
four per-subsystem base types, which each inherited `Exception` directly:
a failed RC call - the single most common way an operation fails - sailed
straight past the handler the docs told callers to write.

None of these types were exported from `rclone_kit` either, so writing the
handler at all meant importing from internal subpackages (`rclone_kit.rc`,
`rclone_kit.native`) whose paths carry no compatibility promise.
"""

import pytest

import rclone_kit
from rclone_kit.exceptions import RcloneKitError
from rclone_kit.native.errors import NativeError
from rclone_kit.optional_dependency import MissingOptionalDependencyError
from rclone_kit.rc.errors import RcCallError
from rclone_kit.rc.jobs import RcJobNotFoundError
from rclone_kit.runtime.exceptions import RcloneRuntimeError

SUBSYSTEM_ROOTS = [RcCallError, NativeError, RcJobNotFoundError, RcloneRuntimeError]
SUBSYSTEM_ROOT_IDS = ["rc_call", "native", "rc_job_not_found", "runtime"]

# Every exception name the curated public API promises. Importing one of
# these from `rclone_kit` is the supported way to write a handler; the
# defining module stays available for detail, but is not the promise.
EXPORTED_EXCEPTION_NAMES = [
    "ConfigParseError",
    "FilesystemError",
    "HttpFetchError",
    "JobExpiredError",
    "JobIdentityError",
    "JobRuntimeClosedError",
    "MissingOptionalDependencyError",
    "NativeError",
    "OperationCancelledError",
    "OperationError",
    "OperationFailedError",
    "OperationShutdownError",
    "OperationStartError",
    "OperationTimeoutError",
    "RcCallError",
    "RcloneCommandError",
    "RcloneKitError",
    "RcloneRuntimeError",
    "S3UploadError",
]


@pytest.mark.parametrize("subsystem_root", SUBSYSTEM_ROOTS, ids=SUBSYSTEM_ROOT_IDS)
def test_every_subsystem_root_is_caught_by_the_library_root(
    subsystem_root: type[Exception],
) -> None:
    assert issubclass(subsystem_root, RcloneKitError)


def test_a_failed_rc_call_is_caught_by_the_documented_boundary_handler() -> None:
    """The exact handler `production_usage.md` recommends, against the
    error an ordinary RC failure actually raises."""
    with pytest.raises(RcloneKitError):
        raise RcCallError("operations/list", 500, {"error": "directory not found"})


@pytest.mark.parametrize("name", EXPORTED_EXCEPTION_NAMES)
def test_every_promised_exception_is_importable_from_the_package_root(name: str) -> None:
    assert name in rclone_kit.__all__
    assert issubclass(getattr(rclone_kit, name), BaseException)


def test_missing_optional_dependency_stays_outside_the_library_root() -> None:
    """A packaging fault, not a storage operation failing.

    `production_usage.md` handles it *before* `except RcloneKitError` and
    re-raises it permanently rather than retrying, which only works while
    the broader handler cannot swallow it first.
    """
    assert issubclass(MissingOptionalDependencyError, ImportError)
    assert not issubclass(MissingOptionalDependencyError, RcloneKitError)
