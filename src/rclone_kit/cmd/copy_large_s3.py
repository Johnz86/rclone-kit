import argparse
from dataclasses import dataclass
from pathlib import Path

from rclone_kit import Rclone, SizeSuffix
from rclone_kit.util import register_signal_cleanup


@dataclass
class Args:
    config_path: Path
    src: str
    dst: str
    chunk_size: SizeSuffix
    threads: int
    retries: int
    save_state_json: Path
    verbose: bool


def list_files(rclone: Rclone, path: str):
    """List files in a remote path."""
    for dirlisting in rclone.walk(path):
        for file in dirlisting.files:
            print(file.path)


def _parse_args() -> Args:
    parser = argparse.ArgumentParser(description="List files in a remote path.")
    parser.add_argument("src", help="File to copy")
    parser.add_argument("dst", help="Destination file")
    parser.add_argument("-v", "--verbose", help="Verbose output", action="store_true")
    parser.add_argument("--config", help="Path to rclone config file", type=Path, required=False)
    parser.add_argument(
        "--chunk-size",
        help="Chunk size that will be read and uploaded in SizeSuffix form, too low or too high will cause issues",
        type=str,
        default="128MB",
    )
    parser.add_argument(
        "--threads",
        help="Max number of chunks to upload in parallel to the destination, each chunk is uploaded in a separate thread",
        type=int,
        default=8,
    )
    parser.add_argument("--retries", help="Number of retries", type=int, default=3)
    parser.add_argument(
        "--resume-json",
        help="Path to resumable JSON file",
        type=Path,
        default="resume.json",
    )

    args = parser.parse_args()
    config: Path | None = args.config
    if config is None:
        config = Path("rclone.conf")
        if not config.exists():
            raise FileNotFoundError(f"Config file not found: {config}")
    assert config is not None
    out = Args(
        config_path=config,
        src=args.src,
        dst=args.dst,
        threads=args.threads,
        chunk_size=SizeSuffix(args.chunk_size),
        retries=args.retries,
        save_state_json=args.resume_json,
        verbose=args.verbose,
    )
    return out


def main() -> int:
    """Main entry point."""
    register_signal_cleanup()
    args = _parse_args()
    rclone = Rclone(rclone_conf=args.config_path)

    err: Exception | None = rclone.copy_file_s3_resumable(
        src=args.src,
        dst=args.dst,
    )
    if err is not None:
        print(f"Error: {err}")
        raise err
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
