"""Owned credentials and reauthentication stop all control workers safely."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from test_runtime import make_runtime

from custom_components import hue_reconfigo as integration
from custom_components.hue_reconfigo.api import BridgeAuthenticationError, BridgeError


class PrivateHueEntry:
    @property
    def data(self):
        raise AssertionError("The official Hue credentials must never be read")


def test_credentials_use_only_own_entry(monkeypatch):
    runtime, _ = make_runtime(monkeypatch, {})
    runtime.entry.data = {
        "hue_entry_id": "official-hue",
        "host": "192.0.2.12",
        "api_key": "owned-application-key",
    }
    lookup = Mock(return_value=PrivateHueEntry())
    runtime.hass.config_entries = SimpleNamespace(async_get_entry=lookup)

    assert runtime.credentials() == ("192.0.2.12", "owned-application-key")
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "own_data",
    [
        {},
        {"host": "192.0.2.12"},
        {"api_key": "owned-key"},
        {"host": "", "api_key": "owned-key"},
        {"host": "192.0.2.12", "api_key": ""},
        {"host": 123, "api_key": "owned-key"},
        {"host": "192.0.2.12", "api_key": None},
    ],
)
def test_missing_owned_credentials_never_fall_back_to_hue(monkeypatch, own_data):
    runtime, _ = make_runtime(monkeypatch, {})
    runtime.entry.data = {"hue_entry_id": "official-hue", **own_data}
    before = dict(runtime.entry.data)
    lookup = Mock(return_value=PrivateHueEntry())
    runtime.hass.config_entries = SimpleNamespace(async_get_entry=lookup)

    with pytest.raises(BridgeAuthenticationError):
        runtime.credentials()

    assert runtime.entry.data == before
    lookup.assert_not_called()


async def test_legacy_setup_requests_pairing_before_runtime_or_bridge_calls(monkeypatch):
    runtime_factory = Mock()
    monkeypatch.setattr(integration, "Runtime", runtime_factory)
    entry = SimpleNamespace(
        entry_id="same-entry",
        data={"hue_entry_id": "official-hue", "controllers_initialized": True},
        options={"adaptive_lighting": True, "polling_mode": "active"},
        subentries={"controller-a": {"enabled": True, "lights": ["light.room"]}},
    )
    before = deepcopy(vars(entry))
    hass = SimpleNamespace()

    with pytest.raises(ConfigEntryAuthFailed, match="Pair Hue Reconfigo"):
        await integration.async_setup_entry(hass, entry)

    runtime_factory.assert_not_called()
    assert vars(entry) == before


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BridgeAuthenticationError("Key rejected"), ConfigEntryAuthFailed),
        (BridgeError("Bridge temporarily unreachable"), ConfigEntryNotReady),
    ],
)
async def test_setup_classifies_auth_failure_separately_from_connection_failure(
    monkeypatch, error, expected
):
    runtime = SimpleNamespace(initialize=AsyncMock(side_effect=error))
    factory = Mock(return_value=runtime)
    monkeypatch.setattr(integration, "Runtime", factory)
    hass = SimpleNamespace(data={"hrc_motion_frontend": True})
    entry = SimpleNamespace(
        data={"hue_entry_id": "official-hue", "host": "192.0.2.12", "api_key": "owned"}
    )

    with pytest.raises(expected):
        await integration.async_setup_entry(hass, entry)

    runtime.initialize.assert_awaited_once()
    assert not hasattr(entry, "runtime_data")


async def test_auth_failure_requests_reauth_once_and_cleanup_is_idempotent(monkeypatch):
    runtime, _ = make_runtime(monkeypatch, {})
    runtime.entry.async_start_reauth = Mock()
    runtime.motion_unsubscribe = Mock()
    runtime.unsubscribe = Mock()
    runtime.motion.close = AsyncMock()
    runtime.dimmer.close = AsyncMock()
    runtime.percentage.close = AsyncMock()
    created = []

    def track_task(hass, coro, name, **kwargs):
        task = asyncio.create_task(coro)
        created.append(task)
        return task

    runtime.entry.async_create_background_task = track_task
    pending_action = asyncio.create_task(asyncio.Event().wait())
    runtime.action_tasks.add(pending_action)

    runtime.authentication_failed()
    runtime.authentication_failed()
    await asyncio.gather(runtime.close(), runtime.close(), *created)
    runtime.authentication_failed()

    runtime.entry.async_start_reauth.assert_called_once_with(runtime.hass)
    runtime.motion_unsubscribe.assert_called_once()
    runtime.unsubscribe.assert_called_once()
    runtime.motion.close.assert_awaited_once()
    runtime.dimmer.close.assert_awaited_once()
    runtime.percentage.close.assert_awaited_once()
    assert runtime.closed and not runtime.available
    assert pending_action.cancelled()
    assert len(created) == 1


@pytest.mark.parametrize("worker", ["rotary", "button_poll", "stream_bootstrap", "stream"])
async def test_rejected_key_stops_worker_without_network_retry(monkeypatch, worker):
    runtime, _ = make_runtime(monkeypatch, {"polling_mode": "continuous"})
    runtime.entry.async_start_reauth = Mock()
    rejected = AsyncMock(side_effect=BridgeAuthenticationError("Key rejected"))
    stream_calls = []
    created = []

    def track_task(hass, coro, name, **kwargs):
        task = asyncio.create_task(coro)
        created.append(task)
        return task

    runtime.entry.async_create_background_task = track_task

    async def rejected_stream():
        stream_calls.append(1)
        raise BridgeAuthenticationError("Key rejected")
        yield  # Keep the same async-iterator protocol as the real SSE adapter.

    if worker == "rotary":
        runtime.client.rotary_resources = rejected
        runtime.task = asyncio.create_task(runtime.poll())
        task = runtime.task
    elif worker == "button_poll":
        runtime.client.button_resources = rejected
        runtime.button_poll_task = asyncio.create_task(runtime.poll_buttons())
        task = runtime.button_poll_task
    else:
        runtime.client.button_resources = (
            rejected if worker == "stream_bootstrap" else AsyncMock(return_value=[])
        )
        runtime.client.button_events = rejected_stream
        runtime.button_task = asyncio.create_task(runtime.listen_buttons())
        task = runtime.button_task

    await asyncio.wait_for(task, timeout=1)
    await runtime.close()
    await asyncio.gather(*created)

    runtime.entry.async_start_reauth.assert_called_once_with(runtime.hass)
    assert runtime.closed and not runtime.available
    if worker == "stream":
        assert stream_calls == [1]
        runtime.client.button_resources.assert_awaited_once()
    else:
        rejected.assert_awaited_once()
        assert not stream_calls
    assert runtime.error_count == 0 and runtime.button_error_count == 0
