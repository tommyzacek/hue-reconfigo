"""Exercise link-button registration without any household credentials."""

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.hue_reconfigo.api import BridgeAuthenticationError, BridgeError
from custom_components.hue_reconfigo.pairing import (
    DEVICE_TYPE,
    LinkButtonNotPressed,
    WrongBridge,
    async_pair_bridge,
    normalize_bridge_id,
    normalize_host,
)


def response(payload, status=200):
    reply = MagicMock(status=status)
    reply.json = AsyncMock(return_value=payload)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=reply)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def session(payload=None, status=200):
    value = MagicMock()
    value.get.return_value = response({"bridgeid": "AABBCCFFFEDDEEFF"})
    value.post.return_value = response(
        [{"success": {"username": "own-reconfigo-key"}}] if payload is None else payload,
        status,
    )
    return value


async def test_pairing_verifies_identity_and_creates_separate_application_key():
    api = session()
    assert await async_pair_bridge(api, "bridge.local", "aa:bb:cc:dd:ee:ff") == "own-reconfigo-key"
    assert api.get.call_args.args == ("https://bridge.local/api/config",)
    assert api.post.call_args.args == ("https://bridge.local/api",)
    assert api.post.call_args.kwargs["json"] == {"devicetype": DEVICE_TYPE}
    assert "headers" not in api.post.call_args.kwargs
    assert api.post.call_args.kwargs["allow_redirects"] is False
    assert api.get.call_args.kwargs["allow_redirects"] is False
    assert api.post.call_args.kwargs["timeout"].total == 10


async def test_new_pairing_without_known_id_does_not_send_any_old_key():
    api = session()
    assert await async_pair_bridge(api, "192.0.2.1") == "own-reconfigo-key"
    api.get.assert_not_called()
    assert set(api.post.call_args.kwargs) == {"json", "timeout", "allow_redirects"}


async def test_unpressed_link_button_keeps_registration_retryable():
    api = session([{"error": {"type": 101, "description": "link button not pressed"}}])
    with pytest.raises(LinkButtonNotPressed):
        await async_pair_bridge(api, "bridge.local", "aabbccddeeff")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        [None],
        [{"success": {}}],
        [{"success": {"username": ""}}],
        [{"success": {"username": "key\r\nsecret"}}],
        [{"error": {"type": 99, "description": "sensitive bridge text"}}],
    ],
)
async def test_malformed_or_unknown_pairing_response_never_exposes_bridge_text(payload):
    with pytest.raises(BridgeError) as exc:
        await async_pair_bridge(session(payload), "bridge.local")
    assert "sensitive" not in str(exc.value)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("identity", [{"bridgeid": "001122fffe334455"}, {}, [], {"bridgeid": None}])
async def test_wrong_or_missing_bridge_identity_cannot_register_a_key(identity):
    api = session()
    api.get.return_value = response(identity)
    with pytest.raises(WrongBridge):
        await async_pair_bridge(api, "bridge.local", "aabbccddeeff")
    api.post.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
async def test_pairing_rejects_auth_status_before_parsing_response(status):
    api = session(status=status)
    api.post.return_value.__aenter__.return_value.json.side_effect = ValueError("not json")
    with pytest.raises(BridgeAuthenticationError):
        await async_pair_bridge(api, "bridge.local")


async def test_pairing_handles_legacy_unauthorized_response():
    api = session([{"error": {"type": 1, "description": "unauthorized user"}}])
    with pytest.raises(BridgeAuthenticationError):
        await async_pair_bridge(api, "bridge.local")


@pytest.mark.parametrize("status", [301, 302, 404, 429, 500])
async def test_pairing_does_not_accept_redirects_or_failed_registration(status):
    with pytest.raises(BridgeError):
        await async_pair_bridge(session(status=status), "bridge.local")


@pytest.mark.parametrize(
    "error",
    [TimeoutError("secret key"), aiohttp.ClientError("secret key"), ValueError("secret key")],
)
async def test_pairing_connection_and_decode_failures_are_sanitized(error):
    api = session()
    api.post.return_value.__aenter__.side_effect = error
    with pytest.raises(BridgeError, match="Cannot communicate") as exc:
        await async_pair_bridge(api, "bridge.local")
    assert "secret" not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize(
    "host",
    [
        "https://bridge.local",
        "user:secret@bridge.local",
        "bridge.local/path",
        "bridge.local?key=secret",
        "bridge.local:443",
        "bridge.local#fragment",
        "bridge.local\n",
        "-bridge.local",
        "bridge..local",
        "[bad]",
        "fe80::1%eth0",
        None,
        "",
    ],
)
def test_host_rejects_url_parts_and_unsafe_input(host):
    with pytest.raises(BridgeError, match="Invalid Hue bridge host"):
        normalize_host(host)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("Bridge.Local", "bridge.local"),
        ("192.0.2.1", "192.0.2.1"),
        ("2001:db8::1", "[2001:db8::1]"),
        ("[2001:db8::1]", "[2001:db8::1]"),
    ],
)
def test_safe_host_normalization(host, expected):
    assert normalize_host(host) == expected


def test_bridge_ids_match_official_hue_normalization():
    assert normalize_bridge_id("AABBCCFFFEDDEEFF") == "aabbccddeeff"
    assert normalize_bridge_id("aa:bb:cc:dd:ee:ff") == "aabbccddeeff"
