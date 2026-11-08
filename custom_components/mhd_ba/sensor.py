"""Next departure sensor (timestamp) with the upcoming departures in attributes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import MhdConfigEntry, signal_update
from .const import (
    CONF_MAX_DEPARTURES,
    CONF_STOP_ID,
    DEFAULT_MAX_DEPARTURES,
    DOMAIN,
    REFRESH_SECONDS,
)


def device_info(entry: MhdConfigEntry) -> DeviceInfo:
    stop_id = entry.data[CONF_STOP_ID]
    return DeviceInfo(
        identifiers={(DOMAIN, str(stop_id))},
        name=entry.title,
        manufacturer="DPB / imhd.sk",
        model="Departure board",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=f"https://imhd.sk/ba/online-zastavkova-tabula?st={stop_id}",
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: MhdConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([NextDepartureSensor(entry)])


class NextDepartureSensor(SensorEntity):
    """State = time of the next departure (realtime prediction when available)."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_departure"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # The list changes with every push; the recorder keeps only state + summary.
    _unrecorded_attributes = frozenset({"departures", "info", "platforms"})

    def __init__(self, entry: MhdConfigEntry) -> None:
        self._entry = entry
        self._runtime = entry.runtime_data
        self._limit = int(entry.options.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES))
        self._attr_unique_id = f"{entry.data[CONF_STOP_ID]}_next_departure"
        self._attr_device_info = device_info(entry)
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry.entry_id), self._handle_update
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_tick, timedelta(seconds=REFRESH_SECONDS)
            )
        )
        self._handle_update()

    @callback
    def _handle_tick(self, _now: datetime) -> None:
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        board = self._runtime.board
        deps = board.departures(datetime.now(timezone.utc), self._limit)
        nxt = deps[0] if deps else None
        attrs: dict[str, Any] = {
            "stop_id": self._entry.data[CONF_STOP_ID],
            "source": nxt.source if nxt else None,
            "line": nxt.line if nxt else None,
            "destination": nxt.destination if nxt else None,
            "platform": (nxt.platform_label or nxt.platform) if nxt else None,
            "delay": nxt.delay if nxt else None,
            "realtime": nxt.realtime if nxt else None,
            "departures": [d.as_dict() for d in deps],
            "platforms": board.platforms.as_dict(),
        }
        if self._runtime.client.info:
            attrs["info"] = self._runtime.client.info
        value = nxt.time if nxt else None
        if value == self._attr_native_value and attrs == self._attr_extra_state_attributes:
            return
        self._attr_native_value = value
        self._attr_extra_state_attributes = attrs
        if self.hass is not None:
            self.async_write_ha_state()
