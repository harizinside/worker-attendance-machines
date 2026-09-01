#!/usr/bin/env python3
"""CLI tool manajemen data mesin absensi ZKTeco.

Subcommands:
    fetch       - Tarik log dari mesin, simpan lokal, push ke CMS
    export      - Export data lokal ke CSV
    delete      - Hapus log di mesin (guarded)
    status      - Ringkasan status per mesin
    sync-users  - Push daftar karyawan dari CMS ke mesin
    scan        - Cari mesin ZKTeco di jaringan LAN
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
import net_scan
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


# Kode punch dari mesin ZKTeco (lihat zk_client.AttendanceRecord.status /
# CMS STATUS_TO_PUNCH): 0/4 = masuk, 1/5 = keluar, 2/3 = istirahat.
STATUS_LABELS = {
    0: "Masuk",
    1: "Keluar",
    2: "Istirahat Keluar",
    3: "Istirahat Masuk",
    4: "Masuk",
    5: "Keluar",
}


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
    machine_name: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    out_file: str,
) -> None:
    """Export attendance data to CSV.

    If machine_name is None, export all machines into one CSV (with a
    'machine' column to distinguish rows). If date_from/date_to are None,
    that bound is not applied (both None = all dates). Query
    store.query_for_export per machine, write CSV with headers:
    machine, finger_id, punch_time, status, keterangan
    """
    conn = store.get_connection(config["db_path"])
    try:
        store.init_db(conn)

        machines = get_machines(config, machine_name)

        total_rows = 0
        out_path = Path(out_file)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["machine", "finger_id", "punch_time", "status", "keterangan"]
            )
            for machine in machines:
                serial = machine["serial_number"]
                rows = store.query_for_export(conn, serial, date_from, date_to)
                for row in rows:
                    punch_time = row["punch_time"]
                    if isinstance(punch_time, datetime):
                        punch_str = punch_time.isoformat()
                    else:
                        punch_str = str(punch_time)
                    status = row["status"]
                    keterangan = STATUS_LABELS.get(status, "?")
                    writer.writerow(
                        [machine["name"], row["finger_id"], punch_str, status, keterangan]
                    )
                total_rows += len(rows)

        date_range = f"{date_from or '...'} to {date_to or '...'}" if (date_from or date_to) else "all dates"

        if total_rows == 0:
            out_path.unlink(missing_ok=True)
            scope = f"{machine_name}" if machine_name else "semua mesin"
            print(f"No attendance records found for {scope} ({date_range})")
            return

        scope = f"'{machine_name}'" if machine_name else f"{len(machines)} mesin"
        print(f"Exported {total_rows} records for {scope} ({date_range}) to {out_file}")

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


def match_scan_results(found: list[dict], machines: list[dict]) -> list[dict]:
    """Cocokkan hasil scan jaringan dengan mesin yang sudah terdaftar di config.

    Args:
        found: list of {"ip", "port", "serial_number", "device_name"} hasil scan.
        machines: config["machines"].

    Returns:
        List baris untuk ditampilkan, tiap item = found item + key "status".
    """
    by_serial = {m["serial_number"]: m for m in machines}

    rows = []
    for item in found:
        serial = item.get("serial_number")
        matched = by_serial.get(serial) if serial else None

        if matched is None:
            status = "Belum terdaftar"
        elif matched["ip"] == item["ip"]:
            status = f"Terdaftar ({matched['name']})"
        else:
            status = (
                f"IP BERUBAH — config: {matched['ip']}, ditemukan: {item['ip']} "
                f"({matched['name']})"
            )

        rows.append({**item, "status": status})

    return rows


def cmd_scan(config: dict, subnet: Optional[str] = None, port: int = 4370) -> None:
    """Scan jaringan LAN untuk cari mesin ZKTeco dan cocokkan dengan config.

    1. Tentukan subnet (arg eksplisit atau auto-detect dari IP lokal).
    2. TCP port sweep via net_scan.scan_port.
    3. Untuk tiap IP yang portnya kebuka, identify_device buat konfirmasi +
       ambil serial_number/device_name.
    4. Cocokkan terhadap config["machines"] dan print sebagai tabel.
    """
    if subnet is None:
        subnet = net_scan.get_local_subnet_prefix()

    if subnet is None:
        print(
            "Error: Gagal deteksi subnet lokal otomatis. "
            "Isi manual, mis. --subnet 192.168.1",
            file=sys.stderr,
        )
        return

    print(f"Scanning {subnet}.0/24 port {port}...")
    open_ips = net_scan.scan_port(subnet, port)

    if not open_ips:
        print("Tidak ada mesin ditemukan.")
        return

    found = []
    for ip in open_ips:
        result = zk_client.identify_device(ip, port)
        if result.success and isinstance(result.data, dict):
            serial_number = result.data.get("serial_number")
            device_name = result.data.get("device_name")
        else:
            serial_number = None
            device_name = None
        found.append(
            {
                "ip": ip,
                "port": port,
                "serial_number": serial_number,
                "device_name": device_name,
            }
        )

    rows = match_scan_results(found, config["machines"])

    header = f"{'IP':<16} {'Port':<6} {'Serial':<20} {'Device Name':<20} Status"
    separator = "-" * len(header)
    print(separator)
    print(header)
    print(separator)
    for row in rows:
        serial = row["serial_number"] or "?"
        device_name = row["device_name"] or "?"
        print(
            f"{row['ip']:<16} {row['port']:<6} {serial:<20} {device_name:<20} "
            f"{row['status']}"
        )
    print(separator)


# --- Interactive menu ---


def interactive_menu(config: dict) -> None:
    """Menu interaktif ala CLI lama (pilih 1, 2, ... lalu isi input)."""
    menu = (
        "\n=== Worker Attendance Machines ===\n"
        "1. Fetch      - Tarik log dari mesin\n"
        "2. Export     - Export data ke CSV\n"
        "3. Delete     - Hapus log di mesin\n"
        "4. Status     - Status mesin\n"
        "5. Sync Users - Sync karyawan ke mesin\n"
        "6. Scan       - Cari mesin ZKTeco di jaringan\n"
        "0. Keluar"
    )
    while True:
        print(menu)
        choice = input("Pilih menu: ").strip()

        if choice == "1":
            machine = input("Nama mesin (kosongkan = semua): ").strip() or None
            cmd_fetch(config, machine)
        elif choice == "2":
            machine = input("Nama mesin (kosongkan = semua): ").strip() or None
            date_from = input("Tanggal awal (YYYY-MM-DD, kosongkan = semua): ").strip() or None
            date_to = input("Tanggal akhir (YYYY-MM-DD, kosongkan = semua): ").strip() or None
            out = input("File output CSV: ").strip()
            cmd_export(config, machine, date_from, date_to, out)
        elif choice == "3":
            machine = input("Nama mesin: ").strip()
            force = input("Force hapus walau ada unsynced? (y/N): ").strip().lower() == "y"
            cmd_delete(config, machine, force)
        elif choice == "4":
            machine = input("Nama mesin (kosongkan = semua): ").strip() or None
            cmd_status(config, machine)
        elif choice == "5":
            machine = input("Nama mesin: ").strip()
            cmd_sync_users(config, machine)
        elif choice == "6":
            subnet = input("Subnet (kosongkan = auto-detect, format 192.168.1): ").strip() or None
            cmd_scan(config, subnet)
        elif choice == "0":
            break
        else:
            print("Pilihan tidak valid.")


# --- Main ---


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if len(sys.argv) == 1:
        interactive_menu(load_config())
        return

    parser = argparse.ArgumentParser(
        description="Tool manajemen mesin absensi ZKTeco"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = subparsers.add_parser("fetch", help="Tarik log dari mesin")
    p_fetch.add_argument("--machine", help="Nama mesin (default: semua)")

    # export
    p_export = subparsers.add_parser("export", help="Export data ke CSV")
    p_export.add_argument("--machine", help="Nama mesin (default: semua)")
    p_export.add_argument(
        "--from", dest="date_from", help="Tanggal awal (YYYY-MM-DD, default: semua tanggal)"
    )
    p_export.add_argument(
        "--to", dest="date_to", help="Tanggal akhir (YYYY-MM-DD, default: semua tanggal)"
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

    # scan
    p_scan = subparsers.add_parser("scan", help="Cari mesin ZKTeco di jaringan")
    p_scan.add_argument(
        "--subnet", help="Subnet 3-oktet, mis. 192.168.1 (default: auto-detect)"
    )
    p_scan.add_argument(
        "--port", type=int, default=4370, help="Port yang di-scan (default: 4370)"
    )

    args = parser.parse_args()
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
    elif args.command == "scan":
        cmd_scan(config, args.subnet, args.port)


if __name__ == "__main__":
    main()
