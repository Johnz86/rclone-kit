"""Unit tests for `rclone_kit.operations.transfer_options`."""

import pytest

from rclone_kit.operations.transfer_options import TransferOptions, encode_transfer_options_config


def test_defaults_encode_to_an_empty_config() -> None:
    assert encode_transfer_options_config(TransferOptions()) == {}


def test_each_field_maps_to_its_exact_go_config_key() -> None:
    options = TransferOptions(
        checkers=8,
        transfers=4,
        low_level_retries=10,
        retries=3,
    )

    assert encode_transfer_options_config(options) == {
        "Checkers": 8,
        "Transfers": 4,
        "LowLevelRetries": 10,
        "Retries": 3,
    }


def test_only_explicitly_set_fields_are_included() -> None:
    options = TransferOptions(transfers=16)

    assert encode_transfer_options_config(options) == {"Transfers": 16}


def test_multi_thread_streams_also_sets_multi_thread_set() -> None:
    options = TransferOptions(multi_thread_streams=4)

    assert encode_transfer_options_config(options) == {
        "MultiThreadStreams": 4,
        "MultiThreadSet": True,
    }


def test_create_empty_src_dirs_is_not_part_of_config_encoding() -> None:
    options = TransferOptions(checkers=8, create_empty_src_dirs=True)

    assert "createEmptySrcDirs" not in encode_transfer_options_config(options)
    assert "CreateEmptySrcDirs" not in encode_transfer_options_config(options)
    assert options.create_empty_src_dirs is True


@pytest.mark.parametrize(
    "field_name", ["checkers", "transfers", "low_level_retries", "retries", "multi_thread_streams"]
)
def test_zero_is_rejected(field_name: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        TransferOptions(**{field_name: 0})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field_name", ["checkers", "transfers", "low_level_retries", "retries", "multi_thread_streams"]
)
def test_negative_is_rejected(field_name: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        TransferOptions(**{field_name: -1})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field_name", ["checkers", "transfers", "low_level_retries", "retries", "multi_thread_streams"]
)
def test_bool_is_rejected_even_though_it_is_technically_an_int(field_name: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        TransferOptions(**{field_name: True})  # type: ignore[arg-type]


def test_none_is_valid_for_every_field() -> None:
    options = TransferOptions(
        checkers=None,
        transfers=None,
        low_level_retries=None,
        retries=None,
        multi_thread_streams=None,
    )
    assert encode_transfer_options_config(options) == {}


def test_files_from_fields_map_to_their_exact_go_config_key() -> None:
    options = TransferOptions(
        retries_sleep="10s",
        timeout="5m",
        max_backlog=5000,
        metadata=True,
    )

    assert encode_transfer_options_config(options) == {
        "RetriesInterval": "10s",
        "Timeout": "5m",
        "MaxBacklog": 5000,
        "Metadata": True,
    }


def test_metadata_false_is_still_encoded_explicitly() -> None:
    options = TransferOptions(metadata=False)

    assert encode_transfer_options_config(options) == {"Metadata": False}


@pytest.mark.parametrize("field_name", ["max_backlog"])
def test_max_backlog_zero_is_rejected(field_name: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        TransferOptions(**{field_name: 0})  # type: ignore[arg-type]
