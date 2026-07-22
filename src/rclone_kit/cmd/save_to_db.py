import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rclone_kit.client import Rclone
from rclone_kit.env_file import load_env_file
from rclone_kit.util import register_signal_cleanup, validate_config_path_exists


class SavesToDatabase(Protocol):
    def save_to_db(
        self,
        src: str,
        db_url: str,
        max_depth: int = -1,
        fast_list: bool = False,
    ) -> None: ...


def _db_url_from_env_or_raise() -> str:
    load_env_file(Path(".env"))
    db_url = os.getenv("DB_URL")
    if db_url is None:
        raise ValueError("DB_URL not set")
    return db_url


@dataclass(frozen=True, slots=True)
class Args:
    config: Path
    path: str
    db_url: str
    fast_list: bool

    def __post_init__(self):
        validate_config_path_exists(self.config)


def fill_db(rclone: SavesToDatabase, path: str, db_url: str, fast_list: bool) -> None:
    """List files in a remote path."""
    rclone.save_to_db(src=path, db_url=db_url, fast_list=fast_list)


def _parse_args() -> Args:
    parser = argparse.ArgumentParser(description="List files in a remote path.")
    parser.add_argument(
        "--config", help="Path to rclone config file", type=Path, default="rclone.conf"
    )
    parser.add_argument("--db", help="Database URL", type=str, default=None)
    parser.add_argument("--fast-list", help="Use fast list", action="store_true")
    parser.add_argument("path", help="Remote path to list")
    tmp = parser.parse_args()
    return Args(
        config=tmp.config,
        path=tmp.path,
        db_url=tmp.db if tmp.db is not None else _db_url_from_env_or_raise(),
        fast_list=tmp.fast_list,
    )


def main() -> int:
    """Main entry point."""
    register_signal_cleanup()
    args = _parse_args()
    path = args.path
    rclone = Rclone(Path(args.config))
    fill_db(rclone=rclone, path=path, db_url=args.db_url, fast_list=args.fast_list)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
