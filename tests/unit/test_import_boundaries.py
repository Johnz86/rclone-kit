import subprocess
import sys
import textwrap


def test_package_and_operation_imports_are_side_effect_free() -> None:
    script = textwrap.dedent(
        """
        import logging
        import subprocess
        import sys
        import threading

        original_handlers = tuple(logging.getLogger().handlers)
        original_thread_count = threading.active_count()

        class RejectProcess(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                raise AssertionError(f"import started a process: {args!r} {kwargs!r}")

        subprocess.Popen = RejectProcess

        import rclone_kit.operations.config_ops
        import rclone_kit.operations.listing_ops
        import rclone_kit.operations.mount_ops
        import rclone_kit.operations.serve_ops
        import rclone_kit.operations.transfer_ops
        import rclone_kit

        assert "boto3" not in sys.modules
        assert "sqlalchemy" not in sys.modules
        assert threading.active_count() == original_thread_count
        assert tuple(logging.getLogger().handlers) == original_handlers
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
