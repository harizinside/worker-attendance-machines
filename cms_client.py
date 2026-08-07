"""Client HTTP untuk push attendance log dan pull employee data dari deneire-cms."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Union

import requests

logger = logging.getLogger(__name__)


def push_attlog(
    cms_base_url: str,
    serial_number: str,
    logs: list[dict],
) -> tuple[bool, str]:
    """Push attendance logs ke CMS menggunakan format ADMS wire protocol.

    Format: POST /iclock/cdata?SN={serial}&table=ATTLOG
    Body: tab-delimited lines, satu line per record::

        fingerId\\ttimestamp\\tstatusCode

    timestamp format: "YYYY-MM-DD HH:MM:SS"

    Args:
        cms_base_url: Base URL CMS (e.g. "https://cms.example.com")
        serial_number: Serial number mesin (harus match yang terdaftar di CMS)
        logs: List of dicts with keys: finger_id, punch_time (str "YYYY-MM-DD HH:MM:SS"), status (int)

    Returns:
        (success: bool, message: str)
        Success = HTTP 200 + body contains "OK"

    CMS sudah dedup by (machineId, fingerId, punchTime) — retry aman, idempotent.
    """
    if not logs:
        logger.info("No logs to push for serial %s", serial_number)
        return True, "No logs to push"

    # Build tab-delimited body
    lines: list[str] = []
    for log in logs:
        punch_time = log["punch_time"]
        # Normalize: if it's a datetime object, format it; if already a string, ensure correct format
        if isinstance(punch_time, datetime):
            ts_str = punch_time.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(punch_time, str):
            # Try to parse common formats then re-format to canonical
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(punch_time, fmt)
                    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    break
                except ValueError:
                    continue
            else:
                ts_str = punch_time
        else:
            ts_str = str(punch_time)

        lines.append(f"{log['finger_id']}\t{ts_str}\t{log['status']}")

    body = "\n".join(lines)
    url = f"{cms_base_url.rstrip('/')}/iclock/cdata"
    params = {"SN": serial_number, "table": "ATTLOG"}

    headers = {"Content-Type": "text/plain"}

    try:
        logger.debug(
            "POST %s SN=%s table=ATTLOG (%d records)",
            cms_base_url,
            serial_number,
            len(logs),
        )
        resp = requests.post(url, params=params, data=body, headers=headers, timeout=30)

        if resp.status_code == 200 and "OK" in resp.text:
            logger.info(
                "Successfully pushed %d logs for serial %s",
                len(logs),
                serial_number,
            )
            return True, "OK"

        logger.warning(
            "CMS returned status %d for serial %s: %s",
            resp.status_code,
            serial_number,
            resp.text[:500],
        )
        return False, f"HTTP {resp.status_code}: {resp.text[:500]}"

    except requests.Timeout:
        msg = f"Timeout connecting to CMS at {cms_base_url}"
        logger.error(msg)
        return False, msg
    except requests.ConnectionError as exc:
        msg = f"Connection error to CMS at {cms_base_url}: {exc}"
        logger.error(msg)
        return False, msg
    except requests.RequestException as exc:
        msg = f"Request failed to CMS at {cms_base_url}: {exc}"
        logger.error(msg)
        return False, msg


def get_provisionable_employees(
    cms_base_url: str,
    serial_number: str,
) -> tuple[bool, list[dict] | str]:
    """Ambil daftar karyawan yang boleh didaftarkan ke mesin ini.

    GET {cms_base_url}/iclock/employees?SN={serial_number}

    Response JSON: [{"fingerId": "EMP001", "name": "Budi"}, ...]

    Args:
        cms_base_url: Base URL CMS
        serial_number: Serial number mesin

    Returns:
        (success, data) where data is list[dict] on success or error message string on failure.
        Each dict has keys: fingerId, name
    """
    url = f"{cms_base_url.rstrip('/')}/iclock/employees"
    params = {"SN": serial_number}

    try:
        logger.debug(
            "GET %s SN=%s employees",
            cms_base_url,
            serial_number,
        )
        resp = requests.get(url, params=params, timeout=30)

        if resp.status_code != 200:
            msg = f"HTTP {resp.status_code}: {resp.text[:500]}"
            logger.warning("get_provisionable_employees failed for serial %s: %s", serial_number, msg)
            return False, msg

        data = resp.json()

        if not isinstance(data, list):
            msg = f"Expected JSON array but got {type(data).__name__}"
            logger.error(msg)
            return False, msg

        # Validate each entry has required keys
        validated: list[dict] = []
        for emp in data:
            if not isinstance(emp, dict):
                logger.warning("Skipping non-dict employee entry: %s", emp)
                continue
            if "fingerId" not in emp or "name" not in emp:
                logger.warning("Skipping employee entry missing fingerId/name: %s", emp)
                continue
            validated.append({
                "fingerId": str(emp["fingerId"]),
                "name": str(emp["name"]),
            })

        logger.info(
            "Retrieved %d provisionable employees for serial %s",
            len(validated),
            serial_number,
        )
        return True, validated

    except requests.Timeout:
        msg = f"Timeout connecting to CMS at {cms_base_url}"
        logger.error(msg)
        return False, msg
    except requests.ConnectionError as exc:
        msg = f"Connection error to CMS at {cms_base_url}: {exc}"
        logger.error(msg)
        return False, msg
    except requests.RequestException as exc:
        msg = f"Request failed to CMS at {cms_base_url}: {exc}"
        logger.error(msg)
        return False, msg
    except ValueError as exc:
        msg = f"Failed to parse JSON response: {exc}"
        logger.error(msg)
        return False, msg
