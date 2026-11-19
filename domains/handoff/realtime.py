"""realtime.py - Instant delivery through Laravel Reverb (naheedopsv2).

Reverb speaks the Pusher protocol, so NiaBot needs no websocket server of its
own - only two signed operations, implemented here with `requests`/`hmac`:

  publish()   - Pusher HTTP API "trigger event" (customer wrote -> agent)
  authorize() - signs a customer's subscription to private-handoff.{id}

Events are signals only ("handoff X changed"); browsers then fetch through the
normal endpoints, so the DB stays the single source of truth. Every function
here is best-effort: if Reverb is unreachable the message is already saved and
the other side catches up by fallback polling.

Event contract (shared with naheedopsv2's App\\Events\\CsrChat\\HandoffUpdated):
  name     handoff.updated
  channels private-handoff.{handoff_id}   customer widget
           private-csr.agent.{agent_id}   assigned agent
  data     {"handoff_id", "type": "message"|"status", "status"}
"""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlencode

import requests

from config.handoff import (
    REVERB_APP_ID,
    REVERB_APP_KEY,
    REVERB_APP_SECRET,
    REVERB_CLIENT_HOST,
    REVERB_CLIENT_PORT,
    REVERB_CLIENT_SCHEME,
    REVERB_HOST,
    REVERB_PORT,
    REVERB_SCHEME,
    REVERB_TIMEOUT,
)
from utils.logger import get_logger

logger = get_logger(__name__)

EVENT_NAME = "handoff.updated"
_SOCKET_ID_RE = re.compile(r"^\d+\.\d+$")
_HANDOFF_CHANNEL_RE = re.compile(r"^private-handoff\.(\d+)$")


def enabled() -> bool:
    return bool(REVERB_APP_ID and REVERB_APP_KEY and REVERB_APP_SECRET and REVERB_HOST)


def client_config() -> dict | None:
    """What the widget needs to open the socket (no secret), or None."""
    if not enabled() or not REVERB_CLIENT_HOST:
        return None
    return {
        "key": REVERB_APP_KEY,
        "host": REVERB_CLIENT_HOST,
        "port": REVERB_CLIENT_PORT,
        "scheme": REVERB_CLIENT_SCHEME,
    }


def handoff_channel(handoff_id: int) -> str:
    return f"private-handoff.{int(handoff_id)}"


def agent_channel(agent_id: int) -> str:
    return f"private-csr.agent.{int(agent_id)}"


def handoff_id_from_channel(channel_name: str) -> int | None:
    match = _HANDOFF_CHANNEL_RE.match(channel_name or "")
    return int(match.group(1)) if match else None


def notify(handoff: dict, change_type: str = "message") -> None:
    """Signals a handoff change to its customer channel and assigned agent."""
    channels = [handoff_channel(handoff["id"])]
    if handoff.get("agent_id"):
        channels.append(agent_channel(handoff["agent_id"]))
    publish(channels, {"handoff_id": handoff["id"], "type": change_type, "status": handoff.get("status")})


def publish(channels: list[str], data: dict) -> bool:
    """Pusher HTTP API: POST /apps/{app_id}/events, signed with the app secret."""
    if not enabled():
        return False

    path = f"/apps/{REVERB_APP_ID}/events"
    body = json.dumps({"name": EVENT_NAME, "channels": channels, "data": json.dumps(data)}, separators=(",", ":"))
    params = {
        "auth_key": REVERB_APP_KEY,
        "auth_timestamp": str(int(time.time())),
        "auth_version": "1.0",
        "body_md5": hashlib.md5(body.encode()).hexdigest(),
    }
    query = urlencode(sorted(params.items()))
    to_sign = f"POST\n{path}\n{query}"
    signature = hmac.new(REVERB_APP_SECRET.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    url = f"{REVERB_SCHEME}://{REVERB_HOST}:{REVERB_PORT}{path}?{query}&auth_signature={signature}"

    try:
        res = requests.post(url, data=body, headers={"Content-Type": "application/json"}, timeout=REVERB_TIMEOUT)
        if res.ok:
            return True
        logger.warning(f"Reverb publish failed: HTTP {res.status_code} {res.text[:200]}")
    except Exception as e:
        logger.warning(f"Reverb publish failed: {e}")
    return False


def authorize(socket_id: str, channel_name: str) -> dict | None:
    """Pusher private-channel auth: {"auth": "<key>:<hmac(secret, socket_id:channel)>"}.
    The caller must already have checked that the customer owns the handoff."""
    if not enabled() or not _SOCKET_ID_RE.match(socket_id or "") or not handoff_id_from_channel(channel_name):
        return None
    signature = hmac.new(
        REVERB_APP_SECRET.encode(), f"{socket_id}:{channel_name}".encode(), hashlib.sha256
    ).hexdigest()
    return {"auth": f"{REVERB_APP_KEY}:{signature}"}
