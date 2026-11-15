"""The PID monitor: an admin-only sidebar panel.

The page (www/panel.html) runs in an iframe panel and borrows the HA frontend's
websocket connection (`window.parent.hassConnection`), so it needs no token.
It draws everything from the recorder: the climate entity's attributes carry
every control cycle (error, P, I, E, raw and sent setpoints, holds...), so no
extra storage is kept here. `pid_climate/rooms` tells the page which entities
belong to each room. The page is served with `no-store`: editing it only needs
a page reload.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import voluptuous as vol
from aiohttp import web

from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from . import const as c

PANEL_URL = "pid-climate"
PAGE_URL = "/pid_climate_panel"
PAGE_FILE = Path(__file__).parent / "www" / "panel.html"
_DONE = f"{c.DOMAIN}_panel_registered"


class PanelPage(HomeAssistantView):
    """The panel page. No data in it (that comes over the websocket), so no auth."""

    url = PAGE_URL
    name = "pid_climate:panel"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        html = await hass.async_add_executor_job(PAGE_FILE.read_text, "utf-8")
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store"})


@callback
def async_setup_panel(hass: HomeAssistant) -> None:
    """Register the page, the panel and the command, once."""
    if hass.data.get(_DONE):
        return
    hass.data[_DONE] = True
    hass.http.register_view(PanelPage())
    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="PID",
        sidebar_icon="mdi:chart-bell-curve-cumulative",
        frontend_url_path=PANEL_URL,
        config={"url": PAGE_URL},
        require_admin=True,
        update=True,
    )
    websocket_api.async_register_command(hass, ws_rooms)


def _seconds(td) -> float:
    return td.total_seconds()


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "pid_climate/rooms"})
@callback
def ws_rooms(hass: HomeAssistant, connection, msg) -> None:
    """Each regulated room: its entities, intervals and tuning."""
    reg = er.async_get(hass)
    rooms = []
    for entry_id, ent in (hass.data.get(c.DOMAIN) or {}).items():
        if not hasattr(ent, "controller") or ent.entity_id is None:
            continue
        pi = ent.controller
        rooms.append({
            "entry_id": entry_id,
            "entity_id": ent.entity_id,
            "name": ent.device_info.get("name") if ent.device_info else ent.entity_id,
            "room_sensor": ent._room_sensor,
            "outdoor_sensor": ent._outdoor_sensor,
            "target_entity": ent._target_entity,
            "high_power_switch": ent._high_power_switch,
            "eight_deg_switch": ent._eight_deg_switch,
            "sampling_period": _seconds(ent._sampling_period),
            "min_setpoint_interval": _seconds(ent._min_setpoint_interval),
            "resync_interval": _seconds(ent._resync_interval),
            "command_min": ent._command_min,
            "command_max": ent._command_max,
            "command_step": ent._command_step,
            "modes": {m: asdict(pi.config(m)) for m in pi.modes},
            # Buttons and diagnostic sensors, by key: {"force_update": "button.salon_force_update"}
            "entities": {
                e.unique_id.removeprefix(f"{ent.unique_id}_"): e.entity_id
                for e in er.async_entries_for_config_entry(reg, entry_id)
                if ent.unique_id and e.unique_id.startswith(f"{ent.unique_id}_")
            },
        })
    rooms.sort(key=lambda r: r["name"])
    connection.send_result(msg["id"], {"rooms": rooms})
