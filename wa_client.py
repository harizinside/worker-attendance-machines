"""Client WhatsApp via WAHA (WhatsApp HTTP API) untuk alert mesin offline."""

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


def send_text(waha_cfg: dict, text: str) -> tuple[bool, str]:
    """Kirim pesan text via WAHA.

    Args:
        waha_cfg: Dict with keys:
            - api_url: str (e.g. "http://localhost:3001/api")
            - api_key: str (optional, can be empty)
            - session: str (e.g. "default")
            - alert_phone: str (e.g. "628123456789")
        text: Pesan yang dikirim

    Returns:
        (success: bool, message: str)

    Implementation:
        POST {api_url}/sendText
        Headers: X-Api-Key (if api_key is non-empty)
        Body JSON: {
            "session": session,
            "chatId": alert_phone + "@c.us",
            "text": text
        }
    """
    api_url = waha_cfg.get("api_url", "")
    api_key = waha_cfg.get("api_key", "")
    session = waha_cfg.get("session", "default")
    alert_phone = waha_cfg.get("alert_phone", "")

    if not api_url or not alert_phone:
        msg = "waha_cfg requires 'api_url' and 'alert_phone'"
        logger.error(msg)
        return False, msg

    chat_id = f"{alert_phone}@c.us"
    url = f"{api_url.rstrip('/')}/sendText"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    payload: dict[str, Any] = {
        "session": session,
        "chatId": chat_id,
        "text": text,
    }

    try:
        logger.debug("POST %s session=%s chatId=%s", api_url, session, chat_id)
        resp = requests.post(url, json=payload, headers=headers, timeout=30)

        if resp.status_code == 200:
            logger.info("WAHA sendText succeeded for chatId=%s", chat_id)
            return True, "OK"

        msg = f"WAHA returned HTTP {resp.status_code}: {resp.text[:500]}"
        logger.warning("WAHA sendText failed: %s", msg)
        return False, msg

    except requests.Timeout:
        msg = f"Timeout connecting to WAHA at {api_url}"
        logger.error(msg)
        return False, msg
    except requests.ConnectionError as exc:
        msg = f"Connection error to WAHA at {api_url}: {exc}"
        logger.error(msg)
        return False, msg
    except requests.RequestException as exc:
        msg = f"Request failed to WAHA at {api_url}: {exc}"
        logger.error(msg)
        return False, msg
