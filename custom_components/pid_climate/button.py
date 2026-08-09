"""One-click versions of the two manual interventions worth having on the device.

Both act on the **active mode**, or on every mode while the entity is off, since
there is no active one to pick. That falls out of passing `active_mode` straight
through: the services read `None` as "all modes".
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import const as c
from .climate import PidClimate, device_info

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PidButtonDescription(ButtonEntityDescription):
    """A button and the coroutine it runs on the climate entity."""

    press: Callable[[PidClimate], Awaitable[None]]


BUTTONS: tuple[PidButtonDescription, ...] = (
    PidButtonDescription(
        key="reset_integral",
        name="reset integral",
        press=lambda pid: pid.async_reset_integral(pid.active_mode),
    ),
    PidButtonDescription(
        key="clear_hold",
        name="clear hold",
        press=lambda pid: pid.async_clear_hold(pid.active_mode),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the manual-intervention buttons for one regulated room."""
    pid = hass.data.get(c.DOMAIN, {}).get(entry.entry_id)
    if pid is None:
        _LOGGER.error("No PID Climate entity registered for %s", entry.title)
        return

    info = device_info(dict(entry.data))
    async_add_entities(
        PidButton(pid, info, description) for description in BUTTONS
    )


class PidButton(ButtonEntity):
    """A manual intervention, one press."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    entity_description: PidButtonDescription

    def __init__(
        self, pid: PidClimate, info: DeviceInfo, description: PidButtonDescription
    ) -> None:
        self._pid = pid
        self.entity_description = description
        self._attr_device_info = info
        if pid.unique_id:
            self._attr_unique_id = f"{pid.unique_id}_{description.key}"

    async def async_press(self) -> None:
        await self.entity_description.press(self._pid)
