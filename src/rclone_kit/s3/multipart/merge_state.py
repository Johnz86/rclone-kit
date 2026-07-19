"""
https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/upload_part_copy.html
  *  client.upload_part_copy

This module provides functionality for S3 multipart uploads, including copying parts
from existing S3 objects using upload_part_copy.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict

from rclone_kit.exceptions import MergeStateError
from rclone_kit.rclone_impl import RcloneImpl
from rclone_kit.s3.multipart.finished_piece import FinishedPiece, FinishedPieceJson


class PartJson(TypedDict):
    part_number: int
    s3_key: str


@dataclass
class Part:
    part_number: int
    s3_key: str

    def to_json(self) -> PartJson:
        return {"part_number": self.part_number, "s3_key": self.s3_key}

    @staticmethod
    def from_json(json_dict: Mapping[str, object]) -> "Part":
        """Raises `MergeStateError` when `part_number`/`s3_key` are missing."""
        part_number = json_dict.get("part_number")
        s3_key = json_dict.get("s3_key")
        if part_number is None or s3_key is None:
            raise MergeStateError(f"{json_dict}")
        assert isinstance(part_number, int)
        assert isinstance(s3_key, str)
        return Part(part_number=part_number, s3_key=s3_key)

    @staticmethod
    def from_json_array(json_array: Sequence[Mapping[str, object]]) -> list["Part"]:
        return [Part.from_json(j) for j in json_array]


class MergeStateJson(TypedDict):
    merge_path: str
    bucket: str
    dst_key: str
    upload_id: str
    finished: list[FinishedPieceJson]
    all: list[PartJson]


class MergeState:
    def __init__(
        self,
        rclone_impl: RcloneImpl,
        merge_path: str,
        upload_id: str,
        bucket: str,
        dst_key: str,
        finished: list[FinishedPiece],
        all_parts: list[Part],
    ) -> None:
        self.rclone_impl: RcloneImpl = rclone_impl
        self.merge_path: str = merge_path
        self.merge_parts_path: str = f"{merge_path}/merge"
        self.upload_id: str = upload_id
        self.bucket: str = bucket
        self.dst_key: str = dst_key
        self.finished: list[FinishedPiece] = list(finished)
        self.all_parts: list[Part] = list(all_parts)

    def on_finished(self, finished_piece: FinishedPiece) -> None:
        self.finished.append(finished_piece)

    def remaining_parts(self) -> list[Part]:
        finished_parts: set[int] = {p.part_number for p in self.finished}
        remaining = [p for p in self.all_parts if p.part_number not in finished_parts]
        return remaining

    @staticmethod
    def from_json(rclone_impl: RcloneImpl, data: MergeStateJson) -> "MergeState":
        """Raises `MergeStateError` when any part entry in `data["all"]` is malformed."""
        finished: list[FinishedPiece] = FinishedPiece.from_json_array(data["finished"])
        all_parts: list[Part] = Part.from_json_array(data["all"])
        return MergeState(
            rclone_impl=rclone_impl,
            merge_path=data["merge_path"],
            upload_id=data["upload_id"],
            bucket=data["bucket"],
            dst_key=data["dst_key"],
            finished=finished,
            all_parts=all_parts,
        )

    def to_json(self) -> MergeStateJson:
        finished = self.finished.copy()
        all_parts = self.all_parts.copy()
        return {
            "merge_path": self.merge_path,
            "bucket": self.bucket,
            "dst_key": self.dst_key,
            "upload_id": self.upload_id,
            "finished": FinishedPiece.to_json_array(finished),
            "all": [part.to_json() for part in all_parts],
        }

    def to_json_str(self) -> str:
        data = self.to_json()
        out = json.dumps(data, indent=2)
        return out

    def __str__(self):
        return self.to_json_str()

    def __repr__(self):
        return self.to_json_str()

    def write(self, rclone_impl: RcloneImpl, dst: str) -> None:
        json_str = self.to_json_str()
        rclone_impl.write_text(dst, json_str)

    def read(self, rclone_impl: RcloneImpl, src: str) -> None:
        json_str = rclone_impl.read_text(src)
        json_dict = json.loads(json_str)
        self.finished = FinishedPiece.from_json_array(json_dict["finished"])
