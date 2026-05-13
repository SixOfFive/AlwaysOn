"""Wake-on-LAN tool — send a magic packet to a known LAN machine.

Host list comes from env var JARVIS_HOSTS as JSON:
    {"bigiron": "AA:BB:CC:DD:EE:FF", "nuc": "11:22:33:44:55:66"}

If JARVIS_HOSTS isn't set, the user's known machines from the LAN are
seeded as a default but with placeholder MACs — they'll need to fill in
actual addresses.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from jarvis_server.tools import Tool

log = logging.getLogger(__name__)

# Default broadcast address. WoL magic packets go to layer-2 broadcast.
# Most home routers will deliver UDP/9 to 255.255.255.255 to the local
# segment; if your network is segmented you may need to set
# JARVIS_WOL_BROADCAST to the directed broadcast for the target subnet.
_DEFAULT_BROADCAST = os.getenv("JARVIS_WOL_BROADCAST", "255.255.255.255")
_DEFAULT_PORT = int(os.getenv("JARVIS_WOL_PORT", "9"))


def _load_hosts() -> dict[str, str]:
    raw = os.getenv("JARVIS_HOSTS")
    if not raw:
        return {}
    try:
        return {k.lower(): v for k, v in json.loads(raw).items()}
    except json.JSONDecodeError as exc:
        log.warning("JARVIS_HOSTS isn't valid JSON: %s", exc)
        return {}


def _parse_mac(mac: str) -> bytes:
    cleaned = mac.replace(":", "").replace("-", "").replace(".", "").strip()
    if len(cleaned) != 12:
        raise ValueError(f"bad MAC: {mac!r}")
    return bytes.fromhex(cleaned)


def _magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + _parse_mac(mac) * 16


def _send_magic(mac: str, broadcast: str = _DEFAULT_BROADCAST, port: int = _DEFAULT_PORT) -> None:
    packet = _magic_packet(mac)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
    finally:
        sock.close()


async def _wake(args: dict[str, Any]) -> str:
    name = str(args.get("host", "")).strip().lower()
    if not name:
        return "I need a machine name to wake."

    hosts = _load_hosts()
    if name not in hosts:
        if not hosts:
            return ("No machines are registered for Wake-on-LAN. "
                    "Set the JARVIS_HOSTS environment variable.")
        known = ", ".join(sorted(hosts.keys()))
        return f"I don't know a machine called {name}. Known: {known}."

    mac = hosts[name]
    try:
        _send_magic(mac)
    except (OSError, ValueError) as exc:
        log.warning("WoL send failed for %s (%s): %s", name, mac, exc)
        return f"Couldn't send the wake packet to {name}: {exc}"
    log.info("WoL: sent magic packet to %s (%s)", name, mac)
    return f"Sent the wake packet to {name}."


def wol_tools() -> list[Tool]:
    return [
        Tool(
            name="wake_on_lan",
            description=(
                "Send a Wake-on-LAN magic packet to a named machine on the "
                "local network. Use when the user says 'wake X', 'turn on "
                "X', 'boot X', etc., where X is a machine name. Only "
                "machines pre-registered in the server's JARVIS_HOSTS env "
                "var will work."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Name of the machine to wake (case-insensitive).",
                    },
                },
                "required": ["host"],
                "additionalProperties": False,
            },
            handler=_wake,
        ),
    ]
