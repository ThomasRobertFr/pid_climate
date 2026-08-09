"""PID Climate: a PI controller that regulates another climate entity.

Configured in YAML under the `climate:` platform key -- see README section 9.
Each block is imported into a config entry so the entities can be grouped under a
device; the entry carries no settings of its own beyond a copy of the YAML.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config import async_hass_config_yaml
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PLATFORM, CONF_UNIQUE_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.BUTTON]
DEPENDENT_PLATFORMS = [Platform.SENSOR, Platform.BUTTON]

SERVICE_RELOAD = "reload"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one regulated room from its imported YAML block."""
    hass.data.setdefault(DOMAIN, {})
    _async_register_reload(hass)
    # Climate first, and awaited on its own: the sensors and buttons read the
    # controller object that the climate platform publishes into hass.data, so
    # setting them all up together would race.
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.CLIMATE])
    await hass.config_entries.async_forward_entry_setups(entry, DEPENDENT_PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear the room down, leaving the underlying AC as it stands."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


def _async_register_reload(hass: HomeAssistant) -> None:
    """Register `pid_climate.reload`, once, however many rooms there are."""
    if hass.services.has_service(DOMAIN, SERVICE_RELOAD):
        return

    async def _handle(call: ServiceCall) -> None:
        await _async_reload_yaml(hass)

    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _handle)


async def _async_reload_yaml(hass: HomeAssistant) -> None:
    """Re-read configuration.yaml and push every block back through import.

    Entries whose data actually changed are updated and reloaded; unchanged ones
    are left alone, so reloading with no edits is a no-op rather than a restart of
    every room.
    """
    # Imported here: climate.py reaches back into this package, so a module-level
    # import would be circular.
    from .climate import PLATFORM_SCHEMA, prepare_import

    try:
        raw_config = await async_hass_config_yaml(hass)
    except HomeAssistantError as err:
        _LOGGER.error("Could not read configuration.yaml: %s", err)
        return

    blocks = [
        block
        for block in cv.ensure_list(raw_config.get(Platform.CLIMATE.value) or [])
        if isinstance(block, dict) and block.get(CONF_PLATFORM) == DOMAIN
    ]

    seen: set[str] = set()
    for block in blocks:
        try:
            validated = PLATFORM_SCHEMA(block)
        except vol.Invalid as err:
            _LOGGER.error(
                "Ignoring invalid pid_climate block %s: %s",
                block.get(CONF_NAME, "<unnamed>"), err,
            )
            continue
        if (data := prepare_import(validated)) is None:
            continue
        seen.add(data[CONF_UNIQUE_ID])
        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=data
        )

    # Blocks that vanished are reported, never deleted: removing an entry would
    # throw away its integrals and per-mode targets, and a block is as likely to
    # be commented out for five minutes as retired for good.
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id not in seen:
            _LOGGER.warning(
                "%s has no YAML block any more and is now running on its stored "
                "config; delete it from Devices & services if that is intended",
                entry.title,
            )
