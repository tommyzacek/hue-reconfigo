"""Hue Reconfigo companion integration."""

from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import BridgeAuthenticationError, BridgeError
from .configuration import controller_binding, initialize_controllers, update_brand_title
from .panel import async_setup_panel, async_unload_panel
from .runtime import Runtime

type HueReconfigoEntry = ConfigEntry[Runtime]


async def async_setup_entry(hass: HomeAssistant, entry: HueReconfigoEntry) -> bool:
    if not all(
        isinstance(entry.data.get(field), str) and entry.data[field]
        for field in ("host", "api_key")
    ):
        # Legacy entries must authorize this integration before any Bridge request.
        raise ConfigEntryAuthFailed("Pair Hue Reconfigo with the Hue Bridge")
    if not hass.data.get("hrc_motion_frontend"):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    "/hue_reconfigo/motion-lux.js",
                    str(Path(__file__).parent / "frontend" / "motion-lux.js"),
                    False,
                )
            ]
        )
        frontend.add_extra_js_url(hass, "/hue_reconfigo/motion-lux.js?v=17")
        hass.data["hrc_motion_frontend"] = True
    runtime = Runtime(hass, entry)
    try:
        await runtime.initialize()
    except BridgeAuthenticationError as err:
        raise ConfigEntryAuthFailed("Pair Hue Reconfigo with the Hue Bridge") from err
    except BridgeError as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = runtime
    update_brand_title(hass, entry)
    initialize_controllers(hass, entry)
    runtime.reload_config = (dict(entry.data), dict(entry.options))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    runtime.start()
    await async_setup_panel(hass)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: HueReconfigoEntry) -> None:
    # Bindings are read live; saving a controller must not discard motion timers.
    if entry.runtime_data.reload_config == (dict(entry.data), dict(entry.options)):
        motion = getattr(entry.runtime_data, "motion", None)
        if motion:
            for resource in tuple(motion.sessions):
                if not controller_binding(entry, resource).get("enabled"):
                    await motion.disable(resource)
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HueReconfigoEntry) -> bool:
    await entry.runtime_data.close()
    async_unload_panel(hass, entry)
    return True
