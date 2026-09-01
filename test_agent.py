"""Tests untuk worker-attendance-machines tool."""

import csv
import io
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
import requests

import agent
import store
import cms_client
import wa_client
import zk_client


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

    def test_should_alert_once_per_incident(self, db):
        """should_alert returns True only once per offline incident."""
        # Simulate 3 consecutive failures with threshold=3
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)

        assert store.should_alert(db, "SN001", 3) is True

        # After recording alert, should NOT alert again
        store.record_alert_sent(db, "SN001")
        assert store.should_alert(db, "SN001", 3) is False

        # More failures still don't trigger new alert (same incident)
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        assert store.should_alert(db, "SN001", 3) is False

    def test_record_alert_sent_preserves_other_columns(self, db):
        """record_alert_sent must not wipe last_fetch_ok_at / consecutive_fail_count.

        Regression test: an earlier implementation used INSERT OR REPLACE with
        only (machine_serial, last_alert_sent_at), which deletes+reinserts the
        row on conflict and silently resets the omitted columns to defaults.
        """
        store.record_fetch_result(db, "SN001", True)  # sets last_fetch_ok_at
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        state_before = store.get_machine_state(db, "SN001")
        assert state_before is not None
        assert state_before["last_fetch_ok_at"] is not None
        assert state_before["consecutive_fail_count"] == 2

        store.record_alert_sent(db, "SN001")

        state_after = store.get_machine_state(db, "SN001")
        assert state_after is not None
        assert state_after["last_alert_sent_at"] is not None
        assert state_after["last_fetch_ok_at"] == state_before["last_fetch_ok_at"]
        assert state_after["consecutive_fail_count"] == 2

    def test_should_alert_resets_after_recovery(self, db):
        """After machine recovers, next offline incident can trigger new alert."""
        # First incident
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        assert store.should_alert(db, "SN001", 3) is True
        store.record_alert_sent(db, "SN001")

        # Machine recovers
        store.record_fetch_result(db, "SN001", True)

        # Second incident
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        store.record_fetch_result(db, "SN001", False)
        assert store.should_alert(db, "SN001", 3) is True


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


# --- WA client tests ---

class TestWaClient:
    """Test WhatsApp client."""

    @patch("wa_client.requests.post")
    def test_send_text(self, mock_post):
        """Send text with correct payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        cfg = {
            "api_url": "http://localhost:3001/api",
            "api_key": "test-key",
            "session": "default",
            "alert_phone": "628123456789",
        }
        success, msg = wa_client.send_text(cfg, "Test alert")

        assert success is True
        call_args = mock_post.call_args
        # Verify URL
        assert "/sendText" in call_args[0][0]
        # Verify body
        body = call_args[1].get("json", {})
        assert body["session"] == "default"
        assert body["chatId"] == "628123456789@c.us"
        assert body["text"] == "Test alert"

    @patch("wa_client.requests.post")
    def test_send_text_no_api_key(self, mock_post):
        """When api_key is empty, no X-Api-Key header is sent."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        cfg = {
            "api_url": "http://localhost:3001/api",
            "api_key": "",
            "session": "default",
            "alert_phone": "628123456789",
        }
        success, msg = wa_client.send_text(cfg, "Test")
        assert success is True
        # Verify no X-Api-Key in headers
        call_args = mock_post.call_args
        headers = call_args[1].get("headers", {})
        assert "X-Api-Key" not in headers or headers.get("X-Api-Key") is None


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
