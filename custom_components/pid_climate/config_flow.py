"""Config flow for pid_climate.

There is no interactive flow. YAML stays the source of truth; each `climate:`
platform block is pushed through an import step. The entry exists purely so the
integration owns a config entry, because Home Assistant devices belong to config
entries and entities set up from a bare YAML platform cannot have one -- see
`entity_platform._async_add_entity`, which skips `device_info` entirely when
`self.config_entry` is None.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME, CONF_UNIQUE_ID

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class PidClimateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Import-only flow backing one YAML block per entry."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Nothing to configure here: the UI cannot create these."""
        return self.async_abort(reason="yaml_only")

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create the entry for a YAML block, or refresh an existing one."""
        unique_id = import_data[CONF_UNIQUE_ID]
        await self.async_set_unique_id(unique_id)

        for entry in self._async_current_entries():
            if entry.unique_id != unique_id:
                continue
            # A wholesale replace, not `_abort_if_unique_id_configured(updates=)`,
            # which merges: a key deleted from YAML has to actually disappear, or
            # dropping `eight_deg_switch` from a block would silently keep working.
            if entry.data != import_data:
                self.hass.config_entries.async_update_entry(entry, data=import_data)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                _LOGGER.info("Reloaded %s from YAML", entry.title)
            return self.async_abort(reason="already_configured")

        _LOGGER.debug("Importing new YAML block %s", unique_id)
        return self.async_create_entry(
            title=import_data[CONF_NAME], data=import_data
        )
