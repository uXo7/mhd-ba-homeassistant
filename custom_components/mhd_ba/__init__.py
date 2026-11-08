"""Bratislava MHD departures: imhd.sk realtime with a local GTFS timetable fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .board import StopBoard
from .const import (
    CONF_GTFS_STOP,
    CONF_STOP_ID,
    DOMAIN,
    GTFS_FILENAME,
    GTFS_REFRESH_HOURS,
    GTFS_URL,
)
from .gtfs import GtfsError, GtfsFeed
from .imhd import ImhdPushClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


def signal_update(entry_id: str) -> str:
    return f"{DOMAIN}_update_{entry_id}"


@dataclass
class Shared:
    """State shared by all stops: one feed, one refresh timer."""

    feed: GtfsFeed
    boards: dict[str, StopBoard] = field(default_factory=dict)  # entry_id -> board
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    unsub_refresh: Any = None


@dataclass
class StopRuntime:
    board: StopBoard
    client: ImhdPushClient


type MhdConfigEntry = ConfigEntry[StopRuntime]


def _executor(hass: HomeAssistant):
    async def run(func, *args):
        return await hass.async_add_executor_job(func, *args)

    return run


async def _shared(hass: HomeAssistant) -> Shared:
    if DOMAIN not in hass.data:
        path = Path(hass.config.path(".storage", GTFS_FILENAME))
        feed = GtfsFeed(path, GTFS_URL)
        await hass.async_add_executor_job(feed.load_meta)
        hass.data[DOMAIN] = Shared(feed)
    return hass.data[DOMAIN]


def _feed_due(feed: GtfsFeed) -> bool:
    return feed.downloaded is None or datetime.now(timezone.utc) - feed.downloaded > timedelta(
        hours=GTFS_REFRESH_HOURS
    )


async def _refresh_feed(hass: HomeAssistant, shared: Shared, force_index: bool = False) -> None:
    """Download when due, (re)index for all configured stops, hand out the index."""
    async with shared.lock:
        changed = False
        if _feed_due(shared.feed):
            changed = await shared.feed.download(
                async_get_clientsession(hass), _executor(hass)
            )
        # All configured stops at once, so parallel entry setups index only once.
        names = {
            e.options.get(CONF_GTFS_STOP, e.data[CONF_GTFS_STOP])
            for e in hass.config_entries.async_entries(DOMAIN)
            if not e.disabled_by
        }
        if not names:
            return
        try:
            await shared.feed.reindex(names, _executor(hass), force=changed or force_index)
        except GtfsError as err:
            shared.feed.last_error = str(err)
            _LOGGER.warning("Timetable unavailable: %s", err)
            return
        for board in shared.boards.values():
            board.index = shared.feed.index
    for entry_id in list(shared.boards):
        async_dispatcher_send(hass, signal_update(entry_id))


async def async_setup_entry(hass: HomeAssistant, entry: MhdConfigEntry) -> bool:
    shared = await _shared(hass)
    stop_id = int(entry.data[CONF_STOP_ID])

    @callback
    def _changed() -> None:
        async_dispatcher_send(hass, signal_update(entry.entry_id))

    client = ImhdPushClient(
        stop_id,
        lambda coro, name: entry.async_create_background_task(hass, coro, name),
        _changed,
        async_get_clientsession(hass),
    )
    board = StopBoard(entry.options.get(CONF_GTFS_STOP, entry.data[CONF_GTFS_STOP]), client)
    shared.boards[entry.entry_id] = board
    entry.runtime_data = StopRuntime(board, client)

    client.start()
    if shared.feed.index is not None:
        board.index = shared.feed.index
    # Download/indexing can take a while on first start; don't block setup.
    entry.async_create_background_task(
        hass, _refresh_feed(hass, shared), f"mhd_ba timetable {stop_id}"
    )

    if shared.unsub_refresh is None:
        async def _tick(_now) -> None:
            await _refresh_feed(hass, shared)

        shared.unsub_refresh = async_track_time_interval(hass, _tick, timedelta(hours=1))

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MhdConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not ok:
        return False
    await entry.runtime_data.client.stop()
    shared: Shared | None = hass.data.get(DOMAIN)
    if shared is not None:
        shared.boards.pop(entry.entry_id, None)
        if not shared.boards:
            if shared.unsub_refresh is not None:
                shared.unsub_refresh()
            hass.data.pop(DOMAIN)
    return True


async def _async_reload(hass: HomeAssistant, entry: MhdConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
