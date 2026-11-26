"""Own-key pairing and in-place recovery exercise actual HA flow helpers."""

from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    ConfigEntries,
    ConfigEntry,
    ConfigEntryState,
    ConfigSubentry,
)
from homeassistant.core import HomeAssistant

from custom_components.hue_reconfigo.api import BridgeError
from custom_components.hue_reconfigo.config_flow import HueReconfigoFlow
from custom_components.hue_reconfigo.const import DOMAIN
from custom_components.hue_reconfigo.pairing import LinkButtonNotPressed, WrongBridge


def make_entry(domain, data, unique_id, options=None):
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain=domain,
        title="Kitchen",
        data=data,
        options=options or {},
        source="user",
        unique_id=unique_id,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )


@pytest.fixture
async def pairing_flow(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    hue = make_entry(
        "hue", {"api_version": 2, "host": "192.0.2.1", "api_key": "official-secret"}, "aabbccddeeff"
    )
    object.__setattr__(hue, "state", ConfigEntryState.LOADED)
    hass.config_entries._entries[hue.entry_id] = hue
    flow = HueReconfigoFlow()
    flow.hass, flow.handler, flow.context = hass, DOMAIN, {"source": "user"}
    with patch(
        "custom_components.hue_reconfigo.config_flow.aiohttp_client.async_get_clientsession",
        return_value=None,
    ):
        yield flow, hue
    await hass.async_block_till_done()
    await hass.async_stop()


async def test_new_entry_is_not_created_before_link_button_pairing(pairing_flow):
    flow, hue = pairing_flow
    result = await flow.async_step_user({"hue_entry_id": hue.entry_id})
    assert result["step_id"] == "pair"
    assert result["data_schema"]({}) == {"host": "192.0.2.1"}
    with patch(
        "custom_components.hue_reconfigo.config_flow.async_pair_bridge",
        AsyncMock(return_value="our-new-key"),
    ) as pair:
        result = await flow.async_step_pair({"host": "192.0.2.2"})
    pair.assert_awaited_once_with(None, "192.0.2.2", hue.unique_id)
    assert result["data"] == {
        "host": "192.0.2.2",
        "api_key": "our-new-key",
        "hue_entry_id": hue.entry_id,
        "bridge_id": hue.unique_id,
    }
    assert result["type"] == "create_entry"
    assert hue.data["api_key"] == "official-secret"
    assert "official-secret" not in str(result)


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (LinkButtonNotPressed("press link"), "link_button_not_pressed"),
        (WrongBridge("wrong bridge"), "wrong_bridge"),
        (BridgeError("private-detail"), "cannot_connect"),
    ],
)
async def test_pairing_failure_keeps_form_open_and_secrets_out(pairing_flow, failure, error):
    flow, hue = pairing_flow
    await flow.async_step_user({"hue_entry_id": hue.entry_id})
    with patch(
        "custom_components.hue_reconfigo.config_flow.async_pair_bridge",
        AsyncMock(side_effect=failure),
    ):
        result = await flow.async_step_pair({"host": "192.0.2.1"})
    assert result["step_id"] == "pair"
    assert result["errors"] == {"base": error}
    assert "private-detail" not in str(result)


async def test_invalid_host_does_not_contact_bridge(pairing_flow):
    flow, hue = pairing_flow
    await flow.async_step_user({"hue_entry_id": hue.entry_id})
    with patch(
        "custom_components.hue_reconfigo.config_flow.async_pair_bridge", AsyncMock()
    ) as pair:
        result = await flow.async_step_pair({"host": "user:password@host/api"})
    assert result["errors"] == {"host": "invalid_host"}
    pair.assert_not_awaited()


@pytest.mark.parametrize("source", ["reauth", "reconfigure"])
async def test_existing_pairing_preserves_ids_options_subentries_and_data(pairing_flow, source):
    flow, hue = pairing_flow
    existing = make_entry(
        DOMAIN,
        {"hue_entry_id": hue.entry_id, "controllers_initialized": True, "custom_value": 7},
        hue.entry_id,
        {"polling_mode": "continuous"},
    )
    flow.hass.config_entries._entries[existing.entry_id] = existing
    sub = ConfigSubentry(
        subentry_type="tap_dial",
        title="Kitchen dial",
        unique_id="dial-a",
        data=MappingProxyType({"resource_id": "dial-a", "enabled": True, "lights": ["light.test"]}),
    )
    flow.hass.config_entries.async_add_subentry(existing, sub)
    saved_options, saved_subentries = dict(existing.options), dict(existing.subentries)
    flow.context = {"source": source, "entry_id": existing.entry_id}
    result = await (
        flow.async_step_reauth(dict(existing.data))
        if source == "reauth"
        else flow.async_step_reconfigure()
    )
    assert result["step_id"] == "pair"
    with (
        patch(
            "custom_components.hue_reconfigo.config_flow.async_pair_bridge",
            AsyncMock(return_value="own-key"),
        ),
        patch.object(
            flow.hass.config_entries, "async_reload", AsyncMock(return_value=True)
        ) as reload,
    ):
        result = await flow.async_step_pair({"host": "192.0.2.2"})
        await flow.hass.async_block_till_done()
    assert result["reason"] == source + "_successful"
    assert existing.data["api_key"] == "own-key"
    assert existing.data["custom_value"] == 7
    assert existing.data["controllers_initialized"] is True
    assert existing.unique_id == hue.entry_id
    assert dict(existing.options) == saved_options
    assert dict(existing.subentries) == saved_subentries
    reload.assert_awaited_once_with(existing.entry_id)
    assert hue.data["api_key"] == "official-secret"


async def test_duplicate_bridge_does_not_pair_again(pairing_flow):
    flow, hue = pairing_flow
    existing = make_entry(DOMAIN, {"hue_entry_id": hue.entry_id}, hue.entry_id)
    flow.hass.config_entries._entries[existing.entry_id] = existing
    from homeassistant.data_entry_flow import AbortFlow

    with pytest.raises(AbortFlow) as abort:
        await flow.async_step_user({"hue_entry_id": hue.entry_id})
    assert abort.value.reason == "already_configured"


async def test_loaded_entry_listener_schedules_exactly_one_reload(pairing_flow):
    from types import SimpleNamespace

    from custom_components.hue_reconfigo import async_reload_entry

    flow, hue = pairing_flow
    existing = make_entry(
        DOMAIN,
        {"hue_entry_id": hue.entry_id, "host": "192.0.2.1", "api_key": "old-own-key"},
        hue.entry_id,
    )
    flow.hass.config_entries._entries[existing.entry_id] = existing
    existing.runtime_data = SimpleNamespace(
        reload_config=(dict(existing.data), dict(existing.options))
    )
    existing.add_update_listener(async_reload_entry)
    flow.context = {"source": "reauth", "entry_id": existing.entry_id}
    await flow.async_step_reauth(dict(existing.data))
    with (
        patch(
            "custom_components.hue_reconfigo.config_flow.async_pair_bridge",
            AsyncMock(return_value="new-own-key"),
        ),
        patch.object(
            flow.hass.config_entries, "async_reload", AsyncMock(return_value=True)
        ) as reload,
    ):
        await flow.async_step_pair({"host": "192.0.2.1"})
        await flow.hass.async_block_till_done()
    reload.assert_awaited_once_with(existing.entry_id)
