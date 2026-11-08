"""Config flow: one entry per stop (imhd.sk stop id + GTFS stop name)."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_GTFS_STOP,
    CONF_MAX_DEPARTURES,
    CONF_STOP_ID,
    DEFAULT_MAX_DEPARTURES,
    DOMAIN,
)
from .gtfs import list_stop_names

BOARD_URL = "https://imhd.sk/ba/online-zastavkova-tabula?st=…"
_ST_RE = re.compile(r"[?&;]st=(\d+)")


def parse_stop_id(value: str) -> int | None:
    """Plain number or any imhd.sk URL with `st=`."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    m = _ST_RE.search(value)
    return int(m.group(1)) if m else None


async def _stop_names(hass: HomeAssistant) -> list[str]:
    """GTFS stop names for the picker; [] when the feed can't be had."""
    from . import _executor, _shared  # noqa: PLC0415 - avoid import cycle

    shared = await _shared(hass)
    feed = shared.feed
    async with shared.lock:
        if not feed.path.exists():
            await feed.download(async_get_clientsession(hass), _executor(hass))
    if not feed.path.exists():
        return []
    try:
        return await hass.async_add_executor_job(list_stop_names, feed.path)
    except Exception:  # noqa: BLE001 - picker is a convenience only
        return []


def _stop_selector(names: list[str]) -> Any:
    if not names:
        return str
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=names, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


def _max_selector() -> Any:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=1, max=30, mode=selector.NumberSelectorMode.BOX)
    )


class MhdConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            stop_id = parse_stop_id(str(user_input[CONF_STOP_ID]))
            if stop_id is None:
                errors[CONF_STOP_ID] = "invalid_stop"
            else:
                await self.async_set_unique_id(str(stop_id))
                self._abort_if_unique_id_configured()
                gtfs_stop = user_input[CONF_GTFS_STOP].strip()
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or gtfs_stop,
                    data={CONF_STOP_ID: stop_id, CONF_GTFS_STOP: gtfs_stop},
                    options={
                        CONF_MAX_DEPARTURES: int(
                            user_input.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES)
                        )
                    },
                )
        names = await _stop_names(self.hass)
        schema = vol.Schema(
            {
                vol.Required(CONF_STOP_ID): str,
                vol.Required(CONF_GTFS_STOP): _stop_selector(names),
                vol.Optional(CONF_NAME): str,
                vol.Optional(CONF_MAX_DEPARTURES, default=DEFAULT_MAX_DEPARTURES): _max_selector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"board_url": BOARD_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MhdOptionsFlow()


class MhdOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_GTFS_STOP: user_input[CONF_GTFS_STOP].strip(),
                    CONF_MAX_DEPARTURES: int(user_input[CONF_MAX_DEPARTURES]),
                }
            )
        names = await _stop_names(self.hass)
        current = entry.options.get(CONF_GTFS_STOP, entry.data[CONF_GTFS_STOP])
        schema = vol.Schema(
            {
                vol.Required(CONF_GTFS_STOP, default=current): _stop_selector(names),
                vol.Required(
                    CONF_MAX_DEPARTURES,
                    default=entry.options.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES),
                ): _max_selector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
