"""Schema and consistency checks for `tests/parity/coverage.toml`, the
machine-readable mirror of the CLI-to-C-ABI migration ledger.

The Markdown ledger (`reference/rclone_cli_to_c_abi_migration_plan.md`) is
the narrative/rationale document; this file is what CI can actually assert
against - every row ID is unique, every status/decision value is one of the
ledger's own defined vocabulary, and a row cannot claim a test-backed status
without naming the test that backs it.
"""

import tomllib
from pathlib import Path
from typing import Any

_COVERAGE_PATH = Path(__file__).resolve().parent.parent / "parity" / "coverage.toml"

_STATUS_ORDER = (
    "planned",
    "adapter_implemented",
    "unit_tested",
    "native_tested",
    "cli_parity_tested",
    "windows_passed",
    "linux_passed",
    "doc_updated",
    "embedded_default",
    "cli_removable",
    "complete",
)
_TEST_BACKED_STATUSES = frozenset(_STATUS_ORDER[_STATUS_ORDER.index("unit_tested") :])
_VALID_DECISIONS = frozenset(
    {
        "direct_rc",
        "composite_rc",
        "bridge",
        "python",
        "deprecate",
        "transitive",
        "remove",
        "runtime",
    }
)
_REQUIRED_FIELDS = frozenset({"id", "method", "decision", "owner_module", "status", "parity_test"})


def _load_rows() -> list[dict[str, Any]]:
    with _COVERAGE_PATH.open("rb") as handle:
        data = tomllib.load(handle)
    return data["row"]


def test_coverage_file_parses_and_is_nonempty() -> None:
    rows = _load_rows()
    assert len(rows) > 0


def test_every_row_has_required_fields() -> None:
    for row in _load_rows():
        missing = _REQUIRED_FIELDS - row.keys()
        assert not missing, f"row {row.get('id')!r} is missing fields: {missing}"


def test_row_ids_are_unique() -> None:
    ids = [row["id"] for row in _load_rows()]
    assert len(ids) == len(set(ids)), "duplicate row id(s) in coverage.toml"


def test_status_values_are_from_the_ledger_checklist() -> None:
    for row in _load_rows():
        assert row["status"] in _STATUS_ORDER, (
            f"row {row['id']!r} has unknown status {row['status']!r}"
        )


def test_decision_values_are_from_the_defined_vocabulary() -> None:
    for row in _load_rows():
        assert row["decision"] in _VALID_DECISIONS, (
            f"row {row['id']!r} has unknown decision {row['decision']!r}"
        )


def test_test_backed_statuses_name_a_parity_test() -> None:
    for row in _load_rows():
        if row["status"] in _TEST_BACKED_STATUSES:
            assert row["parity_test"], (
                f"row {row['id']!r} claims {row['status']!r} but names no test"
            )


def test_named_parity_tests_exist_on_disk() -> None:
    repo_root = _COVERAGE_PATH.parent.parent.parent
    for row in _load_rows():
        parity_test = row["parity_test"]
        if not parity_test:
            continue
        test_path = parity_test.split("::", 1)[0]
        assert (repo_root / test_path).is_file(), (
            f"row {row['id']!r} names a parity test file that does not exist: {test_path}"
        )


def test_windows_or_linux_true_implies_a_test_backed_status() -> None:
    for row in _load_rows():
        if row["windows"] or row["linux"]:
            assert row["status"] in _TEST_BACKED_STATUSES, (
                f"row {row['id']!r} claims a passing platform with no test-backed status"
            )


def test_failure_contract_complete_defaults_true_when_absent() -> None:
    # Only rows with a known, documented gap (T11/T12 per the Wave D design
    # review's F4) should ever set this to False.
    for row in _load_rows():
        if row.get("failure_contract_complete") is False:
            assert row["id"] in {"T11", "T12"}, (
                f"row {row['id']!r} sets failure_contract_complete=False; "
                "confirm this is a deliberately tracked gap, not a stray edit"
            )
