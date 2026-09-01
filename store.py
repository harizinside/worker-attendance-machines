"""SQLite local store for attendance logs and machine state."""

import sqlite3
from datetime import datetime, timezone
from typing import Optional


def get_connection(db_path: str = "attendance.db") -> sqlite3.Connection:
    """Get SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if not exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS attendance_logs (
            machine_serial   TEXT NOT NULL,
            finger_id        TEXT NOT NULL,
            punch_time       TEXT NOT NULL,
            status           INTEGER NOT NULL,
            pushed_to_cms    INTEGER NOT NULL DEFAULT 0,
            fetched_at       TEXT NOT NULL,
            UNIQUE(machine_serial, finger_id, punch_time)
        );

        CREATE TABLE IF NOT EXISTS machine_state (
            machine_serial         TEXT PRIMARY KEY,
            last_fetch_ok_at       TEXT,
            consecutive_fail_count INTEGER NOT NULL DEFAULT 0
        );
    """)


def upsert_logs(
    conn: sqlite3.Connection,
    machine_serial: str,
    logs: list[dict],
) -> int:
    """Insert attendance logs, skip duplicates (INSERT OR IGNORE).

    Each log dict has: finger_id, punch_time (datetime obj), status.
    Returns count of newly inserted rows.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for log in logs:
        rows.append((
            machine_serial,
            str(log["finger_id"]),
            log["punch_time"].isoformat(),
            int(log["status"]),
            now_iso,
        ))

    before_total = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO attendance_logs
            (machine_serial, finger_id, punch_time, status, pushed_to_cms, fetched_at)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        rows,
    )
    # Return count of actually inserted rows (INSERT OR IGNORE skips duplicates)
    return conn.total_changes - before_total


def mark_pushed(
    conn: sqlite3.Connection,
    machine_serial: str,
    log_ids: list[tuple],
) -> None:
    """Mark specific logs as pushed_to_cms=1.

    log_ids is list of (finger_id, punch_time) tuples.
    """
    for finger_id, punch_time in log_ids:
        conn.execute(
            """
            UPDATE attendance_logs
            SET pushed_to_cms = 1
            WHERE machine_serial = ?
              AND finger_id      = ?
              AND punch_time     = ?
            """,
            (machine_serial, str(finger_id), punch_time),
        )


def unsynced_logs(
    conn: sqlite3.Connection,
    machine_serial: str,
) -> list[dict]:
    """Return all logs where pushed_to_cms=0 for this machine.

    Returns list of dicts with: finger_id, punch_time, status.
    """
    cursor = conn.execute(
        """
        SELECT finger_id, punch_time, status
        FROM attendance_logs
        WHERE machine_serial = ? AND pushed_to_cms = 0
        ORDER BY punch_time ASC
        """,
        (machine_serial,),
    )
    return [dict(row) for row in cursor.fetchall()]


def unsynced_count(
    conn: sqlite3.Connection,
    machine_serial: str,
) -> int:
    """Count of logs not yet pushed to CMS."""
    cursor = conn.execute(
        """
        SELECT COUNT(*)
        FROM attendance_logs
        WHERE machine_serial = ? AND pushed_to_cms = 0
        """,
        (machine_serial,),
    )
    return cursor.fetchone()[0]


def query_for_export(
    conn: sqlite3.Connection,
    machine_serial: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    """Query logs for export, filtered by machine and optional date range (inclusive).

    date_from/date_to are 'YYYY-MM-DD' strings. Either or both may be None,
    in which case that bound is not applied (None/None = all dates).
    Returns list of dicts with: finger_id, punch_time, status.
    """
    query = "SELECT finger_id, punch_time, status FROM attendance_logs WHERE machine_serial = ?"
    params: list = [machine_serial]

    if date_from:
        query += " AND punch_time >= ?"
        params.append(date_from)
    if date_to:
        query += " AND punch_time < ? || 'T23:59:59.999999+00:00'"
        params.append(date_to)

    query += " ORDER BY punch_time ASC"

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def record_fetch_result(
    conn: sqlite3.Connection,
    machine_serial: str,
    ok: bool,
) -> None:
    """Update machine_state after a fetch attempt.

    If ok: set last_fetch_ok_at=now, reset consecutive_fail_count=0.
    If not ok: increment consecutive_fail_count by 1.
    Uses an upsert to ensure the row exists.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if ok:
        conn.execute(
            """
            INSERT INTO machine_state
                (machine_serial, last_fetch_ok_at, consecutive_fail_count)
            VALUES (?, ?, 0)
            ON CONFLICT(machine_serial) DO UPDATE SET
                last_fetch_ok_at = excluded.last_fetch_ok_at,
                consecutive_fail_count = 0
            """,
            (machine_serial, now_iso),
        )
    else:
        conn.execute(
            """
            INSERT OR IGNORE INTO machine_state (machine_serial) VALUES (?)
            """,
            (machine_serial,),
        )
        conn.execute(
            """
            UPDATE machine_state
            SET consecutive_fail_count = consecutive_fail_count + 1
            WHERE machine_serial = ?
            """,
            (machine_serial,),
        )


def get_machine_state(
    conn: sqlite3.Connection,
    machine_serial: str,
) -> Optional[dict]:
    """Return machine_state row as dict, or None if not exists."""
    cursor = conn.execute(
        """
        SELECT machine_serial, last_fetch_ok_at, consecutive_fail_count
        FROM machine_state
        WHERE machine_serial = ?
        """,
        (machine_serial,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(row)
