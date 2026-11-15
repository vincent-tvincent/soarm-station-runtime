"""Dashboard telemetry projection; no database connections or robot hardware."""

import copy
import json
from pathlib import Path

import pytest

try:
    from soarm_station_system.dashboard_node import telemetry_view
    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not ROS_AVAILABLE, reason="Sourced ROS environment required"
)


def test_local_history_clears_delivery_queue_without_fabricating_acknowledgments():
    config = {"collector": {"url": ""}}
    upload = {
        "local_committed": 120,
        "remote_committed": 7,
        "queued": 113,
        "error": "Collector endpoint not configured",
    }
    original = copy.deepcopy((config, upload))

    result = telemetry_view(config, upload)

    assert result["delivery_mode"] == "local"
    assert result["queued"] == 0
    assert result["local_committed"] == 120
    assert result["remote_committed"] == 7
    assert result["error"] == ""
    assert (config, upload) == original


@pytest.mark.parametrize("error", [
    "Telemetry storage limit reached", "database or disk is full",
])
def test_local_history_keeps_storage_errors_and_does_not_invent_counts(error):
    result = telemetry_view({}, {"queued": 16, "error": error})

    assert result["delivery_mode"] == "local"
    assert result["queued"] == 0
    assert result["error"] == error
    assert "remote_committed" not in result
    assert "local_committed" not in result


@pytest.mark.parametrize("error", [
    "", "Collector endpoint not configured", "Collector acknowledgment timed out",
])
def test_remote_delivery_preserves_actual_queue_counts_and_error(error):
    upload = {
        "local_committed": 120,
        "remote_committed": 7,
        "queued": 113,
        "error": error,
    }

    result = telemetry_view({"collector": {"url": "https://collector.invalid"}}, upload)

    assert result["delivery_mode"] == "remote"
    assert {key: result[key] for key in upload} == upload


def test_metadata_uses_configured_cadence_and_retention_without_exposing_config(tmp_path):
    database = tmp_path / "private-history.sqlite3"
    database.write_bytes(b"data")
    config = {
        "database": str(database),
        "collector": {
            "url": "https://private-collector.invalid/secret-path",
            "token_env": "PRIVATE_COLLECTOR_TOKEN",
            "ca_file": "/private/certificates/collector.pem",
        },
        "monitoring": {
            "period_s": 2.5,
            "retention_days": 14,
            "storage_limit_mb": 128,
        },
    }

    result = telemetry_view(config, {"queued": 3})

    assert result["sample_period_s"] == 2.5
    assert result["retention_days"] == 14
    assert result["storage_limit_mb"] == 128
    assert result["storage_bytes"] == 4
    encoded = json.dumps(result)
    for private_value in (str(database), *config["collector"].values()):
        assert private_value not in encoded
    assert not {"database", "collector", "url", "token_env", "ca_file"} & result.keys()


def test_unspecified_metadata_has_documented_local_history_defaults():
    result = telemetry_view({}, {})

    assert result["sample_period_s"] == 10
    assert result["retention_days"] == 30
    assert result["storage_limit_mb"] == 512
    assert result["storage_bytes"] is None


def test_storage_includes_database_wal_and_shared_memory_without_opening_sqlite(tmp_path):
    database = tmp_path / "history.sqlite3"
    database.write_bytes(b"d" * 4096)
    Path(str(database) + "-wal").write_bytes(b"w" * 1024)
    Path(str(database) + "-shm").write_bytes(b"s" * 512)

    result = telemetry_view({"database": str(database)}, {})

    assert result["storage_bytes"] == 5632


def test_missing_sidecars_do_not_make_existing_database_size_unknown(tmp_path):
    database = tmp_path / "history.sqlite3"
    database.write_bytes(b"measured")

    assert telemetry_view({"database": database}, {})["storage_bytes"] == 8


def test_missing_database_is_unknown_even_with_an_orphan_sidecar(tmp_path):
    database = tmp_path / "missing.sqlite3"
    Path(str(database) + "-wal").write_bytes(b"orphan")

    assert telemetry_view({"database": str(database)}, {})["storage_bytes"] is None


def test_actual_empty_database_is_known_zero(tmp_path):
    database = tmp_path / "empty.sqlite3"
    database.touch()

    assert telemetry_view({"database": str(database)}, {})["storage_bytes"] == 0


@pytest.mark.parametrize("denied_suffix", ["", "-wal", "-shm"])
def test_inaccessible_database_or_sidecar_is_unknown_not_partial_or_zero(
    tmp_path, monkeypatch, denied_suffix
):
    database = tmp_path / "history.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        Path(str(database) + suffix).write_bytes(b"stored")
    denied = Path(str(database) + denied_suffix)
    original_stat = Path.stat

    def inaccessible_stat(path, *args, **kwargs):
        if path == denied:
            raise PermissionError("Cannot inspect telemetry storage")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible_stat)
    result = telemetry_view(
        {"database": str(database)},
        {"local_committed": 11, "remote_committed": 0, "error": "Storage unavailable"},
    )

    assert result["storage_bytes"] is None
    assert result["local_committed"] == 11
    assert result["remote_committed"] == 0
    assert result["error"] == "Storage unavailable"
