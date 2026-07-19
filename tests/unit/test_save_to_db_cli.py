from typing import Any

from rclone_kit.cmd.save_to_db import fill_db

SUPPLIED_DATABASE_URL = "sqlite:///supplied.db"


class _RecordingRclone:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def save_to_db(
        self,
        src: str,
        db_url: str,
        max_depth: int = -1,
        fast_list: bool = False,
    ) -> None:
        del max_depth
        self.calls.append(
            {
                "src": src,
                "db_url": db_url,
                "fast_list": fast_list,
            }
        )


def test_fill_db_uses_supplied_database_url() -> None:
    rclone = _RecordingRclone()

    fill_db(
        rclone,
        path="remote:bucket",
        db_url=SUPPLIED_DATABASE_URL,
        fast_list=True,
    )

    assert rclone.calls == [
        {
            "src": "remote:bucket",
            "db_url": SUPPLIED_DATABASE_URL,
            "fast_list": True,
        }
    ]
