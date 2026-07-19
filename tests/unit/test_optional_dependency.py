"""Unit tests for `rclone_kit.optional_dependency.MissingOptionalDependencyError`."""

from rclone_kit.optional_dependency import MissingOptionalDependencyError


def test_missing_optional_dependency_error_message_names_extra_and_module() -> None:
    error = MissingOptionalDependencyError("S3 operations", "s3", "boto3")

    assert error.feature_name == "S3 operations"
    assert error.extra_name == "s3"
    assert error.module_name == "boto3"
    assert str(error) == (
        "S3 operations require the 'boto3' package. Install it with: pip install \"rclone-kit[s3]\""
    )


def test_missing_optional_dependency_error_is_an_import_error() -> None:
    assert isinstance(MissingOptionalDependencyError("x", "y", "z"), ImportError)
