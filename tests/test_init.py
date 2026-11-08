"""Integration setup, entities and config flow (network patched out)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mhd_ba.const import (
    CONF_GTFS_STOP,
    CONF_MAX_DEPARTURES,
    CONF_STOP_ID,
    DOMAIN,
    GTFS_FILENAME,
)

from .conftest import GTFS_ZIP, recorded_client


class FakeClient:
    """ImhdPushClient replacement serving a recorded board."""

    instances: list[FakeClient] = []
    fresh_value = True

    def __init__(self, stop_id, create_task, on_change, http_session=None, **_kw):
        rec, self.last = recorded_client(stop_id)
        self.stop_id = stop_id
        self.board = rec.board
        self.vehicles = rec.vehicles
        self.info = []
        self.on_change = on_change
        self.last_update = datetime.fromtimestamp(self.last, timezone.utc)
        self.last_error = None
        self.reconnects = 0
        self.connected = True
        FakeClient.instances.append(self)

    def fresh(self, _age):
        return FakeClient.fresh_value

    def start(self):
        pass

    async def stop(self):
        pass


@pytest.fixture
def feed_on_disk(hass: HomeAssistant):
    path = hass.config.path(".storage", GTFS_FILENAME)
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copy(GTFS_ZIP, path)
    with open(path.replace(".zip", ".json"), "w") as fh:
        json.dump({"downloaded": datetime.now(timezone.utc).isoformat()}, fh)
    yield path


@pytest.fixture
def fake_client():
    FakeClient.instances.clear()
    FakeClient.fresh_value = True
    with patch("custom_components.mhd_ba.ImhdPushClient", FakeClient):
        yield FakeClient


async def _setup(hass, stop_id=324, name="Cintorín Slávičie", title="Slavicie", max_dep=5):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=str(stop_id),
        data={CONF_STOP_ID: stop_id, CONF_GTFS_STOP: name},
        options={CONF_MAX_DEPARTURES: max_dep},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.mhd_ba.gtfs.GtfsFeed.download", return_value=False):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_entities(hass: HomeAssistant, freezer: FrozenDateTimeFactory, feed_on_disk, fake_client):
    from .conftest import recorded_client as rc
    _, last = rc(324)
    freezer.move_to(datetime.fromtimestamp(last, timezone.utc))
    await _setup(hass)
    state = hass.states.get("sensor.slavicie_next_departure")
    assert state is not None, hass.states.async_entity_ids()
    deps = state.attributes["departures"]
    assert len(deps) == 5
    assert state.attributes["source"] == "imhd"
    assert state.attributes["line"] == deps[0]["line"]
    assert deps[0]["platform"] in {"A", "C"}
    assert deps[0]["platform_id"] in {"798", "800"}
    assert datetime.fromisoformat(state.state) == datetime.fromisoformat(deps[0]["time"])
    rt = hass.states.get("binary_sensor.slavicie_realtime")
    assert rt.state == "on"
    assert rt.attributes["timetable_loaded"] is True


async def test_timetable_fallback(hass: HomeAssistant, freezer, feed_on_disk, fake_client):
    from .conftest import recorded_client as rc
    _, last = rc(324)
    freezer.move_to(datetime.fromtimestamp(last, timezone.utc))
    fake_client.fresh_value = False
    await _setup(hass)
    state = hass.states.get("sensor.slavicie_next_departure")
    assert state.attributes["source"] == "gtfs"
    assert all(not d["realtime"] for d in state.attributes["departures"])
    assert hass.states.get("binary_sensor.slavicie_realtime").state == "off"


async def test_unload(hass: HomeAssistant, feed_on_disk, fake_client):
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert DOMAIN not in hass.data


async def test_config_flow(hass: HomeAssistant, feed_on_disk):
    with patch("custom_components.mhd_ba.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert r["type"] == FlowResultType.FORM
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"stop_id": "abc", CONF_GTFS_STOP: "Zoo"})
        assert r["errors"] == {CONF_STOP_ID: "invalid_stop"}
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"],
            {
                CONF_STOP_ID: "https://imhd.sk/ba/online-zastavkova-tabula?st=325&zoom=120",
                CONF_GTFS_STOP: "Zoo",
                "name": "Zoo",
                CONF_MAX_DEPARTURES: 10,
            },
        )
        assert r["type"] == FlowResultType.CREATE_ENTRY
        assert r["data"] == {CONF_STOP_ID: 325, CONF_GTFS_STOP: "Zoo"}
        assert r["options"] == {CONF_MAX_DEPARTURES: 10}
        # duplicate
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {CONF_STOP_ID: "325", CONF_GTFS_STOP: "Zoo"}
        )
        assert r["type"] == FlowResultType.ABORT
