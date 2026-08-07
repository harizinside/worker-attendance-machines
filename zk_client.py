"""Wrapper pyzk: connect ke mesin ZKTeco, operasi attendance & user."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from zk import ZK
from zk.exception import ZKError

logger = logging.getLogger(__name__)

# Device diasumsikan di-set ke waktu lokal WIB, sama seperti asumsi CMS di
# src/routes/iclock/cdata.ts (hardcode "+07:00" saat parse timestamp device).
# ponytail: kalau nanti ada cabang di luar WIB, jadikan ini per-mesin config.
DEVICE_TZ = timezone(timedelta(hours=7))


@dataclass
class DeviceInfo:
    """Info kapasitas device."""

    rec_capacity: int  # kapasitas total log device
    attendance_count: int  # jumlah log saat ini di device
    users_count: int  # jumlah user terdaftar di device


@dataclass
class AttendanceRecord:
    """Satu record absensi dari device."""

    finger_id: str
    punch_time: datetime
    status: int  # 0=in, 1=out, 2=break_out, 3=break_in, 4=in, 5=out (STATUS_TO_PUNCH CMS)


@dataclass
class OperationResult:
    """Hasil operasi device."""

    success: bool
    message: str = ""
    data: object = None  # DeviceInfo, list[AttendanceRecord], dll


def _parse_record(record) -> AttendanceRecord:
    """Parse a raw pyzk attendance record into an AttendanceRecord.

    pyzk's Attendance has two separate fields: `.status` is the verify
    method (password/fingerprint/card), `.punch` is the actual check-type
    (in/out/break) — CMS's STATUS_TO_PUNCH (0=in,1=out,2=break_out,
    3=break_in,4=in,5=out) expects the latter, sent through unmodified
    exactly like a real device would in the ADMS wire format.
    """
    ts = record.timestamp
    if isinstance(ts, datetime):
        punch_time = ts.replace(tzinfo=DEVICE_TZ) if ts.tzinfo is None else ts.astimezone(DEVICE_TZ)
    else:
        # Fallback: treat as unix timestamp
        punch_time = datetime.fromtimestamp(ts, tz=DEVICE_TZ)

    return AttendanceRecord(
        finger_id=str(record.user_id),
        punch_time=punch_time,
        status=int(record.punch),
    )


def connect(ip: str, port: int = 4370, timeout: int = 10):
    """Connect ke device ZKTeco. Return ZK connection object atau raise Exception."""
    zk = ZK(ip, port=port, timeout=timeout)
    try:
        return zk.connect()
    except ZKError as exc:
        raise ConnectionError(f"Failed to connect to {ip}:{port}") from exc


def disconnect(conn) -> None:
    """Disconnect dari device dengan aman (catch exceptions)."""
    try:
        conn.disconnect()
    except Exception:
        logger.warning("Exception during disconnect", exc_info=True)


def pull_logs(ip: str, port: int = 4370, timeout: int = 10) -> OperationResult:
    """Tarik semua log absensi dari device.

    Returns OperationResult with data=list[AttendanceRecord] on success.
    Catches all connection/timeout exceptions, returns success=False with error message.
    One machine failing must NOT crash the whole process.
    """
    conn = None
    try:
        conn = connect(ip, port, timeout)
        attendances = conn.get_attendance()

        records = []
        for raw in attendances:
            try:
                records.append(_parse_record(raw))
            except Exception:
                logger.warning("Skipping malformed attendance record", exc_info=True)

        return OperationResult(success=True, message="OK", data=records)

    except (ConnectionError, ZKError) as exc:
        logger.warning("pull_logs failed for %s:%d — %s", ip, port, exc)
        return OperationResult(success=False, message=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in pull_logs for %s:%d", ip, port)
        return OperationResult(success=False, message=str(exc))
    finally:
        if conn is not None:
            disconnect(conn)


def clear_logs(ip: str, port: int = 4370, timeout: int = 10) -> OperationResult:
    """Hapus SEMUA log di device (CMD_CLEAR_OPLOG - hardware limitation, no date range).

    Returns OperationResult. Catches exceptions.
    """
    conn = None
    try:
        conn = connect(ip, port, timeout)
        conn.clear_attendance()
        return OperationResult(success=True, message="All attendance logs cleared")

    except (ConnectionError, ZKError) as exc:
        logger.warning("clear_logs failed for %s:%d — %s", ip, port, exc)
        return OperationResult(success=False, message=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in clear_logs for %s:%d", ip, port)
        return OperationResult(success=False, message=str(exc))
    finally:
        if conn is not None:
            disconnect(conn)


def get_device_info(ip: str, port: int = 4370, timeout: int = 10) -> OperationResult:
    """Ambil info kapasitas device.

    pyzk tidak punya get_info() — kapasitas didapat lewat read_sizes(), yang
    mengisi atribut rec_cap/records/users di object koneksi.

    Returns OperationResult with data=DeviceInfo on success.
    """
    conn = None
    try:
        conn = connect(ip, port, timeout)
        conn.read_sizes()

        return OperationResult(
            success=True,
            message="OK",
            data=DeviceInfo(
                rec_capacity=int(conn.rec_cap),
                attendance_count=int(conn.records),
                users_count=int(conn.users),
            ),
        )

    except (ConnectionError, ZKError) as exc:
        logger.warning("get_device_info failed for %s:%d — %s", ip, port, exc)
        return OperationResult(success=False, message=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in get_device_info for %s:%d", ip, port)
        return OperationResult(success=False, message=str(exc))
    finally:
        if conn is not None:
            disconnect(conn)


def push_users(
    ip: str,
    users: list[tuple[str, str]],
    port: int = 4370,
    timeout: int = 10,
) -> OperationResult:
    """Push daftar user ke device.

    users: list of (finger_id, name) tuples.
    Strategy: get existing users first, then untuk tiap user cari uid kosong
    berikutnya (re-check tiap iterasi, bukan cuma sekali di awal — gap di uid
    existing bisa bikin uid berikutnya tabrakan kalau cuma increment linear).

    NOTE: uid adalah slot numerik di device (1-65535), user_id adalah string
    yang dipetakan ke fingerId. password wajib string ("") — pyzk manggil
    .encode() di internal set_user, int 0 bikin AttributeError.
    """
    conn = None
    try:
        conn = connect(ip, port, timeout)

        existing_users = conn.get_users()
        used_uids: set[int] = {u.uid for u in existing_users}

        def next_free_uid(start: int) -> int:
            uid = start
            while uid in used_uids:
                uid += 1
            return uid

        pushed = 0
        candidate_uid = 1
        for finger_id, name in users:
            candidate_uid = next_free_uid(candidate_uid)
            try:
                conn.set_user(
                    uid=candidate_uid,
                    name=name,
                    user_id=str(finger_id),
                    password="",
                )
                used_uids.add(candidate_uid)
                pushed += 1
                candidate_uid += 1
            except ZKError as exc:
                logger.warning(
                    "Failed to push user finger_id=%s name=%s — %s",
                    finger_id,
                    name,
                    exc,
                )

        return OperationResult(
            success=True,
            message=f"Pushed {pushed}/{len(users)} users",
            data=pushed,
        )

    except (ConnectionError, ZKError) as exc:
        logger.warning("push_users failed for %s:%d — %s", ip, port, exc)
        return OperationResult(success=False, message=str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in push_users for %s:%d", ip, port)
        return OperationResult(success=False, message=str(exc))
    finally:
        if conn is not None:
            disconnect(conn)
