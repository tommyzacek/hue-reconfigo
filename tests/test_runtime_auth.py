"""Stop native control on revoked keys instead of silently using HA fallback."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.hue_reconfigo.api import BridgeAuthenticationError
from custom_components.hue_reconfigo.runtime import Runtime


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(
        "custom_components.hue_reconfigo.runtime.aiohttp_client.async_get_clientsession",
        lambda *args, **kwargs: None,
    )
    hass = SimpleNamespace(
        services=SimpleNamespace(async_call=AsyncMock()),
        states=SimpleNamespace(
            get=lambda entity: SimpleNamespace(state="on", attributes={"brightness": 128})
        ),
    )
    entry = SimpleNamespace(
        data={},
        options={},
        subentries={},
        async_create_background_task=lambda h, coro, name, **kw: asyncio.create_task(coro),
    )
    result = Runtime(hass, entry)
    result.adaptive.mark = AsyncMock()
    result.authentication_failed = Mock(side_effect=lambda: setattr(result, "closed", True))
    return result


@pytest.mark.parametrize("operation", ["delta", "transition", "stop"])
async def test_native_writes_request_reauth_on_revoked_key(runtime, operation):
    failed = AsyncMock(side_effect=BridgeAuthenticationError("revoked own key"))
    if operation == "delta":
        runtime.client.brightness_delta = failed
        await runtime._send_brightness_delta((("light", "id"),), 5, 500)
    elif operation == "transition":
        runtime.client.brightness_transition = failed
        await runtime._send_hue_transition((("light", "id"),), 50, 500)
    else:
        runtime.client.stop_dimming = failed
        await runtime._stop_hue_transition((("light", "id"),))
    runtime.authentication_failed.assert_called_once()
    runtime.hass.services.async_call.assert_not_awaited()


async def test_held_ramp_auth_failure_never_falls_back_to_ha_brightness(runtime):
    runtime.client.read_brightness = AsyncMock(side_effect=BridgeAuthenticationError("revoked"))
    runtime.read_brightness = Mock(return_value=50)
    await runtime._start_button_ramp(
        ("controller", 1), {"lights": ["light.room"], "resources": (("light", "id"),)}
    )
    runtime.authentication_failed.assert_called_once()
    runtime.read_brightness.assert_not_called()
    runtime.hass.services.async_call.assert_not_awaited()


async def test_motion_ramp_auth_failure_does_not_send_ha_freeze_fallback(runtime):
    runtime.hue_dimming_resources = lambda lights: (("light", "id"),)
    runtime.client.stop_dimming = AsyncMock(side_effect=BridgeAuthenticationError("revoked"))
    runtime.motion.freeze_ramps = AsyncMock()
    await runtime.stop_motion_ramps({"light.room": 50})
    runtime.authentication_failed.assert_called_once()
    runtime.motion.freeze_ramps.assert_not_awaited()


async def test_button_readback_auth_failure_does_not_fall_back_to_ha_turn_off(runtime):
    runtime.motion.expand = lambda lights: lights
    runtime.hue_dimming_resources = lambda lights: (("light", "id"),)
    runtime.client.read_brightness = AsyncMock(side_effect=BridgeAuthenticationError("revoked"))
    runtime.motion.persist = AsyncMock()
    await runtime.light_button_action(["light.room"], {}, {}, "off")
    runtime.authentication_failed.assert_called_once()
    runtime.motion.persist.assert_not_awaited()
    runtime.hass.services.async_call.assert_not_awaited()


async def test_closed_runtime_cannot_send_pending_light_commands(runtime):
    runtime.closed = True
    await runtime.send_brightness(("light.room",), 50, 0.5)
    await runtime.light_button_action(["light.room"], {}, {}, "off")
    runtime.hass.services.async_call.assert_not_awaited()
    runtime.adaptive.mark.assert_not_awaited()
    assert runtime.control_allowed("controller") is False
