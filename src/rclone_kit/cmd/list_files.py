import argparse
from pathlib import Path

from rclone_kit.client import Rclone
from rclone_kit.util import register_signal_cleanup


def list_files(rclone: Rclone, path: str):
    """List files in a remote path."""
    for dirlisting in rclone.walk(path):
        for file in dirlisting.files:
            print(file.path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List files in a remote path.")
    parser.add_argument("--config", help="Path to rclone config file", required=True)
    parser.add_argument("path", help="Remote path to list")
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    register_signal_cleanup()
    args = _parse_args()
    path = args.path
    rclone = Rclone(Path(args.config))
    list_files(rclone, path)
    return 0
