"""Diagnostic: is realtime data from imhd.sk flowing?"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import DOMAIN, MhdConfigEntry, signal_update
from .const import CONF_STOP_ID, REFRESH_SECONDS
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: MhdConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([RealtimeSensor(entry)])


class RealtimeSensor(BinarySensorEntity):
    """On = fresh realtime board; off = timetable fallback."""

    _attr_has_entity_name = True
    _attr_translation_key = "realtime"
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"last_update", "timetable_downloaded"})

    def __init__(self, entry: MhdConfigEntry) -> None:
        self._entry = entry
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.data[CONF_STOP_ID]}_realtime"
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
        client = self._runtime.client
        shared = self.hass.data.get(DOMAIN) if self.hass else None
        feed = shared.feed if shared else None
        index = feed.index if feed else None
        attrs: dict[str, Any] = {
            "last_update": client.last_update.isoformat() if client.last_update else None,
            "reconnects": client.reconnects,
            "last_error": client.last_error,
            "timetable_loaded": index is not None,
            "timetable_valid_until": (
                index.valid_until.isoformat() if index and index.valid_until else None
            ),
            "timetable_downloaded": (
                feed.downloaded.isoformat() if feed and feed.downloaded else None
            ),
            "timetable_error": feed.last_error if feed else None,
        }
        is_on = self._runtime.board.realtime_ok
        if is_on == self._attr_is_on and attrs == self._attr_extra_state_attributes:
            return
        self._attr_is_on = is_on
        self._attr_extra_state_attributes = attrs
        if self.hass is not None:
            self.async_write_ha_state()
