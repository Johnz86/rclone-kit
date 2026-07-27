"""Unit tests for `rclone_kit.operations.transfer_options`."""

from dataclasses import replace

import pytest

from rclone_kit.operations.transfer_options import (
    _NON_NEGATIVE_INT_FIELDS,
    _POSITIVE_INT_FIELDS,
    COPY_TUNED_PROFILE,
    COPY_TUNED_PROFILE_WITHOUT_RETRIES,
    TransferOptions,
    encode_transfer_options_config,
)

_ALL_INT_FIELDS = [*_POSITIVE_INT_FIELDS, *_NON_NEGATIVE_INT_FIELDS]


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


@pytest.mark.parametrize("field_name", _POSITIVE_INT_FIELDS)
def test_zero_is_rejected_for_counts_rclone_cannot_act_on(field_name: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        TransferOptions(**{field_name: 0})  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", _ALL_INT_FIELDS)
def test_negative_is_rejected(field_name: str) -> None:
    with pytest.raises(ValueError, match="integer"):
        TransferOptions(**{field_name: -1})  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", _ALL_INT_FIELDS)
def test_bool_is_rejected_even_though_it_is_technically_an_int(field_name: str) -> None:
    with pytest.raises(ValueError, match="integer"):
        TransferOptions(**{field_name: True})  # type: ignore[arg-type]


def test_multi_thread_streams_zero_disables_multi_thread_transfers() -> None:
    """`--multi-thread-streams 0` is rclone's documented way to switch
    multi-thread transfers off (`fs/operations/multithread.go` bails out on
    `MultiThreadStreams <= 1`), so zero must reach `_config` instead of
    being rejected as an invalid count. `MultiThreadSet` stays `True`: the
    CLI keys it off the flag having been changed, not off its value.
    """
    options = TransferOptions(multi_thread_streams=0)

    assert encode_transfer_options_config(options) == {
        "MultiThreadStreams": 0,
        "MultiThreadSet": True,
    }


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


def test_a_profile_fills_only_the_fields_the_caller_left_unset() -> None:
    tuned = TransferOptions(timeout="5m").with_defaults_from(COPY_TUNED_PROFILE)

    assert tuned == replace(COPY_TUNED_PROFILE, timeout="5m")


def test_an_explicit_value_is_never_overwritten_by_the_profile() -> None:
    """A caller who names a setting must get exactly that setting - a
    profile supplies defaults, it does not impose a policy.
    """
    tuned = TransferOptions(transfers=1).with_defaults_from(COPY_TUNED_PROFILE)

    assert tuned.transfers == 1
    assert tuned.checkers == COPY_TUNED_PROFILE.checkers


def test_options_without_a_profile_stay_empty_so_rclones_own_defaults_apply() -> None:
    """`copy_dir()`/`copy_remote()`/`start_copy()` build a `TransferOptions`
    and never name a profile; the tuned numbers must not reach them as
    field defaults on the type.
    """
    assert encode_transfer_options_config(TransferOptions()) == {}
    assert TransferOptions().with_defaults_from(TransferOptions()) == TransferOptions()


def test_the_retryless_profile_is_the_copy_profile_minus_retries() -> None:
    """`sync()`/`move()` share `copy()`'s tuning but run endpoints with no
    command-level retry loop, so their profile must differ in `retries`
    alone - never in the numbers themselves.
    """
    assert COPY_TUNED_PROFILE.retries is not None
    assert COPY_TUNED_PROFILE_WITHOUT_RETRIES.retries is None
    assert replace(COPY_TUNED_PROFILE, retries=None) == COPY_TUNED_PROFILE_WITHOUT_RETRIES
    assert "Retries" not in encode_transfer_options_config(COPY_TUNED_PROFILE_WITHOUT_RETRIES)
