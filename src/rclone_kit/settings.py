"""Global logging and verbosity settings."""

import os
import warnings

_UPLOAD_PARTS_LOGGING_ENV_VAR = "LOG_UPLOAD_S3_RESUMABLE"
_RCLONE_VERBOSE_ENV_VAR = "RCLONE_KIT_VERBOSE"


def rclone_verbose(value: bool | None, from_api: bool = False) -> bool:
    """Get or set global rclone command verbosity."""
    if not from_api:
        warnings.warn(
            "rclone_verbose is deprecated. Use LogSettings.rclone_verbose instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    if value is not None:
        os.environ[_RCLONE_VERBOSE_ENV_VAR] = "1" if value else "0"
    return bool(int(os.getenv(_RCLONE_VERBOSE_ENV_VAR, "0")))


class LogSettings:
    """Settings for the library's opt-in operation logging."""

    @staticmethod
    def enable_upload_parts_logging(value: bool | None = None) -> bool:
        """Get or set resumable-upload part logging."""
        if value is not None:
            os.environ[_UPLOAD_PARTS_LOGGING_ENV_VAR] = "1" if value else "0"
        env_value = os.getenv(_UPLOAD_PARTS_LOGGING_ENV_VAR, "0")
        return env_value.lower() in {"1", "true", "yes"}

    @staticmethod
    def rclone_verbose(value: bool) -> bool:
        """Enable or disable verbose rclone command logging."""
        return rclone_verbose(value, from_api=True)
