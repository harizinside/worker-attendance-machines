#!/usr/bin/env python3
"""CLI tool manajemen data mesin absensi ZKTeco.

Subcommands:
    fetch       - Tarik log dari mesin, simpan lokal, push ke CMS
    export      - Export data lokal ke CSV
    delete      - Hapus log di mesin (guarded)
    status      - Ringkasan status per mesin
    sync-users  - Push daftar karyawan dari CMS ke mesin
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cms_client
import store
import wa_client
import zk_client
from store import should_alert
from zk_client import DeviceInfo

logger = logging.getLogger(__name__)

# --- Config ---


def load_config(path: str = "config.json") -> dict:
    """Load dan validasi config file.

    Args:
        path: Path ke file config JSON.

    Returns:
        Parsed config dict.

    Raises:
        SystemExit: Jika file tidak ada, tidak valid JSON, atau key wajib hilang.
    """
    config_path = Path(path)

    if not config_path.exists():
        print(f"Error: Config file not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    required_keys = ["cms_base_url", "db_path", "machines"]
    missing = [k for k in required_keys if k not in config]
    if missing:
        print(
            f"Error: Missing required config keys: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(config["machines"], list) or not config["machines"]:
        print("Error: 'machines' must be a non-empty array", file=sys.stderr)
        sys.exit(1)

    # Validate each machine entry
    machine_required = ["name", "ip", "port", "serial_number"]
    for i, machine in enumerate(config["machines"]):
        m_missing = [k for k in machine_required if k not in machine]
        if m_missing:
            print(
                f"Error: Machine[{i}] missing required keys: {', '.join(m_missing)}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Set defaults for optional top-level keys
    config.setdefault("capacity_warning_pct", 90)
    config.setdefault("offline_alert_after_cycles", 3)

    return config


def get_machines(config: dict, machine_name: Optional[str] = None) -> list[dict]:
    """Filter machines from config. If machine_name given, return only that one.

    Args:
        config: Parsed config dict.
        machine_name: Optional machine name to filter by.

    Returns:
        List of machine dicts matching the filter.

    Raises:
        SystemExit: If named machine not found.
    """
    machines = config["machines"]

    if machine_name is None:
        return machines

    for machine in machines:
        if machine["name"] == machine_name:
            return [machine]

    available = ", ".join(m["name"] for m in machines)
    print(
        f"Error: Machine '{machine_name}' not found. Available: {available}",
        file=sys.stderr,
    )
    sys.exit(1)


# --- Subcommands ---


def cmd_fetch(config: dict, machine_name: Optional[str] = None) -> None:
    """Fetch attendance logs from machines.

    Per machine:
    1. pull_logs from device via zk_client
    2. If success: upsert_logs to local store
    3. Push unsynced logs to CMS via cms_client.push_attlog
    4. mark_pushed for successfully pushed logs
    5. get_device_info, check capacity vs capacity_warning_pct, log warning if exceeded
    6. record_fetch_result(ok=True)
    7. If pull_logs failed: record_fetch_result(ok=False)
       - Check should_alert(threshold=offline_alert_after_cycles)
       - If should alert: send WA via wa_client, record_alert_sent
    """
    conn = store.get_connection(config["db_path"])
    try:
        store.init_db(conn)

        machines = get_machines(config, machine_name)
        threshold = config.get("offline_alert_after_cycles", 3)
        capacity_warning_pct = config.get("capacity_warning_pct", 90)
        cms_base_url = config["cms_base_url"]
        waha_cfg = config.get("waha")

        for machine in machines:
            serial = machine["serial_number"]
            ip = machine["ip"]
            port = machine["port"]
            name = machine["name"]

            print(f"\n{'='*60}")
            print(f"Machine: {name} ({serial})")
            print(f"{'='*60}")

            # Step 1: Pull logs from device
            result = zk_client.pull_logs(ip, port)

            if result.success:
                # Step 2: Upsert logs to local store
                records = result.data  # type: ignore[assignment]
                assert isinstance(records, list), "Expected list of AttendanceRecord"
                log_dicts = [
                    {
                        "finger_id": r.finger_id,
                        "punch_time": r.punch_time,
                        "status": r.status,
                    }
                    for r in records
                ]
                inserted = store.upsert_logs(conn, serial, log_dicts)
                print(f"  Pulled {len(records)} records, inserted {inserted} new rows")

                # Step 3: Get unsynced logs and push to CMS
                unsynced = store.unsynced_logs(conn, serial)
                if unsynced:
                    # Format punch_time as "YYYY-MM-DD HH:MM:SS" for CMS
                    formatted_logs = []
                    for log in unsynced:
                        punch_time = log["punch_time"]
                        if isinstance(punch_time, datetime):
                            ts_str = punch_time.strftime("%Y-%m-%d %H:%M:%S")
                        elif isinstance(punch_time, str):
                            ts_str = punch_time
                        else:
                            ts_str = str(punch_time)
                        formatted_logs.append({
                            "finger_id": log["finger_id"],
                            "punch_time": ts_str,
                            "status": log["status"],
                        })

                    push_success, push_msg = cms_client.push_attlog(
                        cms_base_url, serial, formatted_logs
                    )

                    if push_success:
                        # Step 4: Mark pushed
                        log_ids = [
                            (log["finger_id"], log["punch_time"])
                            for log in formatted_logs
                        ]
                        store.mark_pushed(conn, serial, log_ids)
                        print(f"  Pushed {len(formatted_logs)} logs to CMS: {push_msg}")
                    else:
                        print(f"  Failed to push to CMS: {push_msg}")
                else:
                    print("  No unsynced logs to push")

                # Step 5: Check device capacity
                info_result = zk_client.get_device_info(ip, port)
                if info_result.success and isinstance(info_result.data, DeviceInfo):
                    info = info_result.data
                    if info.rec_capacity > 0:
                        usage_pct = info.attendance_count / info.rec_capacity * 100
                        print(
                            f"  Device capacity: {info.attendance_count}/{info.rec_capacity} "
                            f"({usage_pct:.1f}%) — users: {info.users_count}"
                        )
                        if usage_pct >= capacity_warning_pct:
                            logger.warning(
                                "Machine %s (%s) capacity at %.1f%% — approaching limit!",
                                name,
                                serial,
                                usage_pct,
                            )
                            print(
                                f"  WARNING: Capacity at {usage_pct:.1f}% — "
                                f"approaching limit of {capacity_warning_pct}%"
                            )
                    else:
                        print(
                            f"  Device capacity: unknown (rec_capacity={info.rec_capacity})"
                        )
                else:
                    print(f"  Could not get device info: {info_result.message}")

                # Step 6: Record successful fetch
                store.record_fetch_result(conn, serial, True)
                print(f"  Fetch OK")

            else:
                # Step 7: Pull failed
                print(f"  FAILED: {result.message}")
                store.record_fetch_result(conn, serial, False)

                # Check if we should alert
                if should_alert(conn, serial, threshold):
                    last_ok_at = store.get_machine_state(conn, serial)
                    if last_ok_at and last_ok_at.get("last_fetch_ok_at"):
                        last_ok_str = last_ok_at["last_fetch_ok_at"]
                    else:
                        last_ok_str = "never"

                    fc_val = last_ok_at["consecutive_fail_count"] if last_ok_at else 0
                    alert_text = (
                        f"ALERT: Mesin {name} ({serial}) tidak reachable "
                        f"sejak {last_ok_str}. "
                        f"Konsekutif gagal: {fc_val} cycle."
                    )

                    if waha_cfg:
                        ok, wa_msg = wa_client.send_text(waha_cfg, alert_text)
                        if ok:
                            store.record_alert_sent(conn, serial)
                            print(f"  Alert sent via WA: {wa_msg}")
                        else:
                            print(f"  Failed to send WA alert: {wa_msg}")
                    else:
                        print(f"  Would alert: {alert_text}")
                        print(f"  (waha config not set)")
                else:
                    fail_count = store.get_machine_state(conn, serial)
                    fc = fail_count["consecutive_fail_count"] if fail_count else 0
                    print(f"  Consecutive failures: {fc} (alert threshold: {threshold})")

        conn.commit()

    finally:
        conn.close()


def cmd_export(
    config: dict,
    machine_name: str,
    date_from: str,
    date_to: str,
    out_file: str,
) -> None:
    """Export attendance data to CSV.

    Query store.query_for_export, write CSV with headers: finger_id, punch_time, status
    """
    conn = store.get_connection(config["db_path"])
    try:
        store.init_db(conn)

        machines = get_machines(config, machine_name)
        if not machines:
            print(f"Error: Machine '{machine_name}' not found", file=sys.stderr)
            sys.exit(1)

        machine = machines[0]
        serial = machine["serial_number"]

        rows = store.query_for_export(conn, serial, date_from, date_to)

        if not rows:
            print(
                f"No attendance records found for {machine_name} "
                f"between {date_from} and {date_to}"
            )
            return

        out_path = Path(out_file)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["finger_id", "punch_time", "status"])
            for row in rows:
                punch_time = row["punch_time"]
                if isinstance(punch_time, datetime):
                    punch_str = punch_time.isoformat()
                else:
                    punch_str = str(punch_time)
                writer.writerow([row["finger_id"], punch_str, row["status"]])

        print(
            f"Exported {len(rows)} records for '{machine_name}' "
            f"({date_from} to {date_to}) to {out_file}"
        )

    finally:
        conn.close()


def cmd_delete(config: dict, machine_name: str, force: bool = False) -> None:
    """Clear attendance logs on device.

    Guard: check store.unsynced_count > 0 → refuse without --force
    If guard passes: zk_client.clear_logs
    """
    conn = store.get_connection(config["db_path"])
    try:
        store.init_db(conn)

        machines = get_machines(config, machine_name)
        if not machines:
            print(f"Error: Machine '{machine_name}' not found", file=sys.stderr)
            sys.exit(1)

        machine = machines[0]
        serial = machine["serial_number"]
        ip = machine["ip"]
        port = machine["port"]

        # Guard: check unsynced count
        count = store.unsynced_count(conn, serial)
        if count > 0 and not force:
            print(
                f"Error: Ada {count} log belum sync ke CMS. "
                f"Gunakan --force untuk tetap hapus.",
                file=sys.stderr,
            )
            return

        print(f"Deleting attendance logs on {machine_name} ({serial})...")
        result = zk_client.clear_logs(ip, port)

        if result.success:
            print(f"  Success: {result.message}")
        else:
            print(f"  Failed: {result.message}")

    finally:
        conn.close()


def cmd_status(config: dict, machine_name: Optional[str] = None) -> None:
    """Print status table for machines.

    Per machine:
    - Try zk_client.get_device_info → reachable, capacity %, attendance_count
    - store.unsynced_count → unsynced rows
    - store.get_machine_state → last_fetch_ok_at, consecutive_fail_count
    Print as formatted table.
    """
    conn = store.get_connection(config["db_path"])
    try:
        store.init_db(conn)

        machines = get_machines(config, machine_name)

        # Table header
        header = (
            f"{'Machine':<25} {'Reachable':<10} {'Capacity%':<12} "
            f"{'Unsynced':<10} {'Last Fetch':<22} {'Fail Count':<10}"
        )
        separator = "-" * len(header)
        print(separator)
        print(header)
        print(separator)

        for machine in machines:
            serial = machine["serial_number"]
            ip = machine["ip"]
            port = machine["port"]
            name = machine["name"]

            # Truncate name for display
            display_name = name[:24] if len(name) > 24 else name

            # Device info
            reachable = "No"
            capacity_str = "N/A"
            info_result = zk_client.get_device_info(ip, port)
            if info_result.success and isinstance(info_result.data, DeviceInfo):
                info = info_result.data
                reachable = "Yes"
                if info.rec_capacity > 0:
                    pct = info.attendance_count / info.rec_capacity * 100
                    capacity_str = f"{pct:.1f}%"
                else:
                    capacity_str = "0.0%"

            # Unsynced count
            unsynced = store.unsynced_count(conn, serial)

            # Machine state
            state = store.get_machine_state(conn, serial)
            if state:
                last_fetch = state.get("last_fetch_ok_at") or "Never"
                # Trim microseconds and timezone for display
                if isinstance(last_fetch, str) and "." in last_fetch:
                    last_fetch = last_fetch.split(".")[0]
                fail_count = state.get("consecutive_fail_count", 0)
            else:
                last_fetch = "Never"
                fail_count = 0

            row = (
                f"{display_name:<25} {reachable:<10} {capacity_str:<12} "
                f"{unsynced:<10} {last_fetch:<22} {fail_count:<10}"
            )
            print(row)

        print(separator)

    finally:
        conn.close()


def cmd_sync_users(config: dict, machine_name: str) -> None:
    """Sync employees from CMS to device.

    1. cms_client.get_provisionable_employees(cms_base_url, serial_number)
    2. zk_client.push_users(ip, users=[(fingerId, name), ...])
    3. Print count of users pushed
    """
    conn = store.get_connection(config["db_path"])
    try:
        store.init_db(conn)

        machines = get_machines(config, machine_name)
        if not machines:
            print(f"Error: Machine '{machine_name}' not found", file=sys.stderr)
            sys.exit(1)

        machine = machines[0]
        serial = machine["serial_number"]
        ip = machine["ip"]
        port = machine["port"]
        cms_base_url = config["cms_base_url"]

        print(f"Fetching employees from CMS for {machine_name} ({serial})...")
        success, data = cms_client.get_provisionable_employees(cms_base_url, serial)

        if not success:
            print(f"Error: Failed to fetch employees from CMS: {data}")
            return

        assert isinstance(data, list), "Expected list of employee dicts"
        employees: list[dict[str, str]] = data
        if not employees:
            print("No employees found in CMS for this machine.")
            return

        users = [(str(emp["fingerId"]), str(emp["name"])) for emp in employees]
        print(f"Pushing {len(users)} users to {machine_name}...")

        result = zk_client.push_users(ip, users, port)

        if result.success:
            print(f"  {result.message}")
        else:
            print(f"  Failed: {result.message}")

    finally:
        conn.close()


# --- Main ---


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tool manajemen mesin absensi ZKTeco"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = subparsers.add_parser("fetch", help="Tarik log dari mesin")
    p_fetch.add_argument("--machine", help="Nama mesin (default: semua)")

    # export
    p_export = subparsers.add_parser("export", help="Export data ke CSV")
    p_export.add_argument("--machine", required=True, help="Nama mesin")
    p_export.add_argument(
        "--from", dest="date_from", required=True, help="Tanggal awal (YYYY-MM-DD)"
    )
    p_export.add_argument(
        "--to", dest="date_to", required=True, help="Tanggal akhir (YYYY-MM-DD)"
    )
    p_export.add_argument("--out", required=True, help="File output CSV")

    # delete
    p_delete = subparsers.add_parser("delete", help="Hapus log di mesin")
    p_delete.add_argument("--machine", required=True, help="Nama mesin")
    p_delete.add_argument(
        "--force", action="store_true", help="Force hapus walau ada unsynced"
    )

    # status
    p_status = subparsers.add_parser("status", help="Status mesin")
    p_status.add_argument("--machine", help="Nama mesin (default: semua)")

    # sync-users
    p_sync = subparsers.add_parser("sync-users", help="Sync karyawan ke mesin")
    p_sync.add_argument("--machine", required=True, help="Nama mesin")

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()

    # Dispatch to command handler
    if args.command == "fetch":
        cmd_fetch(config, args.machine)
    elif args.command == "export":
        cmd_export(config, args.machine, args.date_from, args.date_to, args.out)
    elif args.command == "delete":
        cmd_delete(config, args.machine, args.force)
    elif args.command == "status":
        cmd_status(config, args.machine)
    elif args.command == "sync-users":
        cmd_sync_users(config, args.machine)


if __name__ == "__main__":
    main()
