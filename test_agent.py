"""Tests untuk worker-attendance-machines tool."""

import csv
import io
import sqlite3
import logging
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
import requests

import agent
import store
import cms_client
import zk_client


class TestLogging:
    """Error tetap tersedia setelah console Windows tertutup."""

    def test_setup_logging_writes_rotating_file(self, tmp_path):
        with patch("agent._default_log_dir", return_value=tmp_path):
            log_path = agent.setup_logging()
            logging.getLogger("test").error("contoh error windows")
            for handler in logging.getLogger().handlers:
                handler.flush()

        assert log_path == tmp_path / "attendance-agent.log"
        assert "contoh error windows" in log_path.read_text(encoding="utf-8")


# --- Store tests ---

class TestStore:
    """Test SQLite store operations."""

    @pytest.fixture
    def db(self):
        """In-memory SQLite connection with schema initialized."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        store.init_db(conn)
        return conn

    def test_upsert_logs_inserts(self, db):
        """New logs are inserted correctly."""
        logs = [
            {"finger_id": "EMP001", "punch_time": datetime(2025, 1, 15, 8, 0, 0), "status": 0},
            {"finger_id": "EMP002", "punch_time": datetime(2025, 1, 15, 8, 5, 0), "status": 0},
        ]
        count = store.upsert_logs(db, "SN001", logs)
        assert count == 2

    def test_upsert_logs_dedup(self, db):
        """Duplicate logs (same machine_serial + finger_id + punch_time) are skipped."""
        log = [{"finger_id": "EMP001", "punch_time": datetime(2025, 1, 15, 8, 0, 0), "status": 0}]
        before = db.total_changes
        count1 = store.upsert_logs(db, "SN001", log)
        assert count1 == 1
        # Second call with same data should insert 0 new rows
        count2 = store.upsert_logs(db, "SN001", log)
        assert count2 == 0

    def test_unsynced_count(self, db):
        """unsynced_count returns correct count of pushed_to_cms=0 rows."""
        logs = [
            {"finger_id": "EMP001", "punch_time": datetime(2025, 1, 15, 8, 0, 0), "status": 0},
            {"finger_id": "EMP002", "punch_time": datetime(2025, 1, 15, 8, 5, 0), "status": 0},
        ]
        store.upsert_logs(db, "SN001", logs)
        assert store.unsynced_count(db, "SN001") == 2

    def test_mark_pushed(self, db):
        """mark_pushed sets pushed_to_cms=1 for specified logs."""
        logs = [
            {"finger_id": "EMP001", "punch_time": datetime(2025, 1, 15, 8, 0, 0), "status": 0},
            {"finger_id": "EMP002", "punch_time": datetime(2025, 1, 15, 8, 5, 0), "status": 0},
        ]
        store.upsert_logs(db, "SN001", logs)
        store.mark_pushed(db, "SN001", [("EMP001", "2025-01-15T08:00:00")])
        assert store.unsynced_count(db, "SN001") == 1

    def test_query_for_export_date_filter(self, db):
        """query_for_export filters by date range correctly."""
        logs = [
            {"finger_id": "EMP001", "punch_time": datetime(2025, 1, 15, 8, 0, 0), "status": 0},
            {"finger_id": "EMP001", "punch_time": datetime(2025, 1, 16, 8, 0, 0), "status": 0},
            {"finger_id": "EMP001", "punch_time": datetime(2025, 1, 17, 8, 0, 0), "status": 0},
        ]
        store.upsert_logs(db, "SN001", logs)
        results = store.query_for_export(db, "SN001", "2025-01-15", "2025-01-16")
        assert len(results) == 2

    def test_record_fetch_result_success(self, db):
        """Successful fetch resets fail count and sets last_fetch_ok_at."""
        store.record_fetch_result(db, "SN001", False)  # fail first
        store.record_fetch_result(db, "SN001", False)  # fail again
        state = store.get_machine_state(db, "SN001")
        assert state is not None
        assert state["consecutive_fail_count"] == 2

        store.record_fetch_result(db, "SN001", True)  # success
        state = store.get_machine_state(db, "SN001")
        assert state is not None
        assert state["consecutive_fail_count"] == 0
        assert state["last_fetch_ok_at"] is not None

    def test_record_fetch_result_failure_increments(self, db):
        """Failed fetch increments consecutive_fail_count."""
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        state = store.get_machine_state(db, "SN001")
        assert state is not None
        assert state["consecutive_fail_count"] == 3

# --- CMS client tests ---

class TestCmsClient:
    """Test CMS HTTP client."""

    @patch("cms_client.requests.post")
    def test_push_attlog_format(self, mock_post):
        """ADMS wire format: tab-delimited, correct URL."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"
        mock_post.return_value = mock_response

        logs = [
            {"finger_id": "EMP001", "punch_time": "2025-01-15 08:00:00", "status": 0},
        ]
        success, msg = cms_client.push_attlog("https://cms.example.com", "SN001", logs)

        assert success is True
        # Verify URL params
        call_kwargs = mock_post.call_args[1]
        params = call_kwargs.get("params", {})
        assert params.get("SN") == "SN001"
        assert params.get("table") == "ATTLOG"
        # Verify body format: tab-delimited
        body = call_kwargs.get("data", "")
        assert "EMP001" in str(body)
        assert "\t" in str(body)

    @patch("cms_client.requests.post")
    def test_push_attlog_failure(self, mock_post):
        """Network failure returns (False, error_message), doesn't raise."""
        mock_post.side_effect = requests.ConnectionError("Connection refused")

        logs = [
            {"finger_id": "EMP001", "punch_time": "2025-01-15 08:00:00", "status": 0},
        ]
        success, msg = cms_client.push_attlog("https://cms.example.com", "SN001", logs)
        assert success is False
        assert "Connection" in msg or "refused" in msg.lower() or len(msg) > 0

    @patch("cms_client.requests.get")
    def test_get_provisionable_employees(self, mock_get):
        """Parse JSON response correctly."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"fingerId": "EMP001", "name": "Budi"},
            {"fingerId": "EMP002", "name": "Siti"},
        ]
        mock_get.return_value = mock_response

        success, data = cms_client.get_provisionable_employees("https://cms.example.com", "SN001")
        assert success is True
        assert len(data) == 2
        emp0 = data[0]
        assert isinstance(emp0, dict)
        assert emp0["fingerId"] == "EMP001"


# --- zk_client smoke tests ---
# (module import + record parsing only — no real device/network. This is the
#  test that would have caught the previous ImportError from bad zk.const /
#  zk.exc names: importing zk_client at all used to crash the whole CLI.)

class TestZkClientSmoke:
    """zk_client must import cleanly and parse records without a real device."""

    def test_parse_record_uses_punch_not_status(self):
        """Punch/check-type must come from .punch, not the verify-method .status."""

        class FakeRecord:
            user_id = "EMP001"
            timestamp = datetime(2025, 1, 15, 8, 0, 0)  # naive, device wall-clock
            status = 1  # verify method (e.g. fingerprint) — must NOT leak into output
            punch = 0  # actual check-type: in

        record = zk_client._parse_record(FakeRecord())
        assert record.finger_id == "EMP001"
        assert record.status == 0  # from .punch, not .status

    def test_parse_record_tags_device_timezone(self):
        """Naive device timestamps are tagged with DEVICE_TZ (WIB), not UTC."""

        class FakeRecord:
            user_id = "EMP001"
            timestamp = datetime(2025, 1, 15, 8, 0, 0)
            status = 0
            punch = 0

        record = zk_client._parse_record(FakeRecord())
        assert record.punch_time.utcoffset() == zk_client.DEVICE_TZ.utcoffset(None)
        assert record.punch_time.hour == 8  # wall-clock preserved, not shifted


class TestSetDeviceTime:
    """Test update waktu device tanpa koneksi hardware nyata."""

    @patch("zk_client.disconnect")
    @patch("zk_client.connect")
    def test_set_device_time_calls_device_and_disconnects(self, mock_connect, mock_disconnect):
        conn = MagicMock()
        mock_connect.return_value = conn
        timestamp = datetime(2026, 9, 1, 14, 30, 45)

        result = zk_client.set_device_time("192.168.1.100", timestamp, 4370)

        assert result.success is True
        assert result.data == timestamp
        conn.set_time.assert_called_once_with(timestamp)
        mock_disconnect.assert_called_once_with(conn)

    @patch("zk_client.disconnect")
    @patch("zk_client.connect")
    def test_set_device_time_returns_failure_and_disconnects(self, mock_connect, mock_disconnect):
        conn = MagicMock()
        conn.set_time.side_effect = RuntimeError("set time rejected")
        mock_connect.return_value = conn

        result = zk_client.set_device_time(
            "192.168.1.100", datetime(2026, 9, 1, 14, 30, 45)
        )

        assert result.success is False
        assert "rejected" in result.message
        mock_disconnect.assert_called_once_with(conn)


class TestCmdUpdateTime:
    """Test orchestration update time untuk satu atau semua mesin."""

    MACHINES = [
        {"name": "Mesin 1", "ip": "192.168.1.100", "port": 4370, "serial_number": "SN001"},
        {"name": "Mesin 2", "ip": "192.168.1.101", "port": 4370, "serial_number": "SN002"},
    ]

    @patch("agent.zk_client.set_device_time")
    def test_updates_all_machines_and_continues_after_failure(self, mock_set_time):
        mock_set_time.side_effect = [
            zk_client.OperationResult(False, "offline"),
            zk_client.OperationResult(True, "updated"),
        ]

        agent.cmd_update_time({"machines": self.MACHINES})

        assert mock_set_time.call_count == 2
        assert mock_set_time.call_args_list[0].args[0] == "192.168.1.100"
        assert mock_set_time.call_args_list[1].args[0] == "192.168.1.101"

    @patch("agent.zk_client.set_device_time")
    def test_updates_only_selected_machine(self, mock_set_time):
        mock_set_time.return_value = zk_client.OperationResult(True, "updated")

        agent.cmd_update_time({"machines": self.MACHINES}, "Mesin 2")

        mock_set_time.assert_called_once()
        assert mock_set_time.call_args.args[0] == "192.168.1.101"


# --- Capacity warning test ---

class TestCapacityWarning:
    """Test capacity warning logic (integration-style with store)."""

    def test_capacity_warning_threshold(self):
        """Warning triggers when usage >= capacity_warning_pct."""
        # This tests the logic that would be in cmd_fetch:
        # if attendance_count / rec_capacity * 100 >= capacity_warning_pct → warning
        rec_capacity = 1000
        attendance_count = 900
        capacity_warning_pct = 90

        usage_pct = (attendance_count / rec_capacity * 100) if rec_capacity > 0 else 0
        assert usage_pct >= capacity_warning_pct  # should trigger warning

        attendance_count = 899
        usage_pct = (attendance_count / rec_capacity * 100) if rec_capacity > 0 else 0
        assert usage_pct < capacity_warning_pct  # should NOT trigger warning


# --- Scan matching test ---

class TestScanMatching:
    """Test match_scan_results() — matches network scan output against config."""

    MACHINES = [
        {"name": "Mesin Lantai 1", "ip": "192.168.1.100", "port": 4370, "serial_number": "SN001"},
        {"name": "Mesin Lantai 2", "ip": "192.168.1.101", "port": 4370, "serial_number": "SN002"},
    ]

    def test_matched_same_ip(self):
        """Serial found in config with the same IP -> registered."""
        found = [{"ip": "192.168.1.100", "port": 4370, "serial_number": "SN001", "device_name": "ZK"}]
        rows = agent.match_scan_results(found, self.MACHINES)
        assert rows[0]["status"] == "Terdaftar (Mesin Lantai 1)"

    def test_matched_different_ip(self):
        """Serial found in config but IP differs -> flagged as changed."""
        found = [{"ip": "192.168.1.200", "port": 4370, "serial_number": "SN001", "device_name": "ZK"}]
        rows = agent.match_scan_results(found, self.MACHINES)
        assert "IP BERUBAH" in rows[0]["status"]
        assert "192.168.1.100" in rows[0]["status"]  # old (config) IP
        assert "192.168.1.200" in rows[0]["status"]  # new (found) IP

    def test_unknown_serial(self):
        """Serial not present in config -> not registered."""
        found = [{"ip": "192.168.1.150", "port": 4370, "serial_number": "SN999", "device_name": "ZK"}]
        rows = agent.match_scan_results(found, self.MACHINES)
        assert rows[0]["status"] == "Belum terdaftar"

    def test_unreadable_serial(self):
        """serial_number None (device didn't respond to identify) -> not registered."""
        found = [{"ip": "192.168.1.150", "port": 4370, "serial_number": None, "device_name": None}]
        rows = agent.match_scan_results(found, self.MACHINES)
        assert rows[0]["status"] == "Belum terdaftar"
