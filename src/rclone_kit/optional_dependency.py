"""Shared exception for a missing optional-extra dependency.

Heavy third-party packages needed only by specific features (`boto3` for S3
operations, `sqlmodel`/`psycopg2-binary` for database operations) live
behind pip extras rather than `[project.dependencies]`, so importing
`rclone_kit` itself never requires them. Every lazy import of one of these
packages catches the resulting bare `ModuleNotFoundError` and re-raises
`MissingOptionalDependencyError` instead, so the caller gets an actionable
message naming the extra to install rather than an opaque import failure.
"""


class MissingOptionalDependencyError(ImportError):
    """Raised when a feature needs an optional-extra dependency that is not
    installed.

    Carries `feature_name` (what the caller was trying to do), `extra_name`
    (the pip extra that provides it), and `module_name` (the underlying
    package whose import failed), so both the error message and programmatic
    handling have everything they need.
    """

    def __init__(self, feature_name: str, extra_name: str, module_name: str) -> None:
        self.feature_name = feature_name
        self.extra_name = extra_name
        self.module_name = module_name
        super().__init__(
            f"{feature_name} require the {module_name!r} package. "
            f'Install it with: pip install "rclone-kit[{extra_name}]"'
        )
