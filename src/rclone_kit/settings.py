"""Global logging and verbosity settings."""

import os

_UPLOAD_PARTS_LOGGING_ENV_VAR = "LOG_UPLOAD_S3_RESUMABLE"
_RCLONE_VERBOSE_ENV_VAR = "RCLONE_KIT_VERBOSE"


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
    def rclone_verbose(value: bool | None = None) -> bool:
        """Get or set verbose rclone logging, backed by `RCLONE_KIT_VERBOSE`.

        Passing `None` reads the current setting without changing it.
        """
        if value is not None:
            os.environ[_RCLONE_VERBOSE_ENV_VAR] = "1" if value else "0"
        return bool(int(os.getenv(_RCLONE_VERBOSE_ENV_VAR, "0")))
