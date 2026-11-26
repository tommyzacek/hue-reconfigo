"""Pair Hue Reconfigo with a bridge using its own application key."""

import ipaddress
import re

import aiohttp

from .api import BridgeError, check_authentication

DEVICE_TYPE = "HueReconfigo#homeassistant"
TIMEOUT = aiohttp.ClientTimeout(total=10)


class LinkButtonNotPressed(BridgeError):
    """Physical bridge link-button authorization is still required."""


class WrongBridge(BridgeError):
    """The host does not identify the bridge selected by the user."""


def normalize_host(host: str) -> str:
    """Accept a host only, without URL syntax, ports or credentials."""
    if not isinstance(host, str) or not host or host != host.strip():
        raise BridgeError("Invalid Hue bridge host")
    address = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        if (
            len(host) > 253
            or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
            or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in host.rstrip(".").split(".")
            )
        ):
            raise BridgeError("Invalid Hue bridge host") from None
        return host.lower()
    # Scope IDs require URL escaping and are not used by the bridge selection flow.
    if "%" in address:
        raise BridgeError("Invalid Hue bridge host")
    return f"[{ip.compressed}]" if ip.version == 6 else ip.compressed


def normalize_bridge_id(bridge_id: str) -> str:
    """Match Hue's MAC, bridge-id and normalized official integration IDs."""
    if not isinstance(bridge_id, str):
        raise WrongBridge("Cannot verify selected Hue bridge")
    value = bridge_id.lower().replace(":", "").replace("-", "")
    if len(value) == 16 and value[6:10] == "fffe":
        value = value[:6] + value[10:]
    if len(value) not in (12, 16) or not re.fullmatch(r"[0-9a-f]+", value):
        raise WrongBridge("Cannot verify selected Hue bridge")
    return value


async def async_pair_bridge(
    session: aiohttp.ClientSession,
    host: str,
    expected_bridge_id: str | None = None,
) -> str:
    """Verify the selected bridge, then create a new app key after a button press.

    No credential from the official Hue integration is accepted or sent. The
    caller supplies HA's session with Hue's self-signed certificate handling.
    Redirects are rejected so a changed endpoint cannot forward pairing data.
    """
    host = normalize_host(host)
    try:
        if expected_bridge_id is not None:
            expected = normalize_bridge_id(expected_bridge_id)
            async with session.get(
                f"https://{host}/api/config", timeout=TIMEOUT, allow_redirects=False
            ) as response:
                check_authentication(response.status)
                if response.status != 200:
                    raise BridgeError("Cannot verify selected Hue bridge")
                identity = await response.json()
            if (
                not isinstance(identity, dict)
                or normalize_bridge_id(identity.get("bridgeid")) != expected
            ):
                raise WrongBridge("The host belongs to a different Hue bridge")
        async with session.post(
            f"https://{host}/api",
            json={"devicetype": DEVICE_TYPE},
            timeout=TIMEOUT,
            allow_redirects=False,
        ) as response:
            check_authentication(response.status)
            if response.status != 200:
                raise BridgeError("Cannot pair with Hue bridge")
            payload = await response.json()
        check_authentication(response.status, payload)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise BridgeError("Invalid Hue pairing response")
        result = payload[0]
        if isinstance(error := result.get("error"), dict):
            if error.get("type") in (101, "101"):
                raise LinkButtonNotPressed("Press the link button on the Hue bridge")
            raise BridgeError("Hue bridge rejected pairing")
        success = result.get("success")
        key = success.get("username") if isinstance(success, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise BridgeError("Invalid Hue pairing response")
        return key
    except aiohttp.ClientError, TimeoutError, ValueError:
        raise BridgeError("Cannot communicate with Hue bridge during pairing") from None
