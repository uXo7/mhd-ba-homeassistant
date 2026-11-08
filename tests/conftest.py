"""Shared fixtures: recorded imhd.sk boards and a trimmed DPB GTFS feed."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.mhd_ba import gtfs
from custom_components.mhd_ba.imhd import Board

pytest_plugins = "pytest_homeassistant_custom_component"

FIXTURES = Path(__file__).parent / "fixtures"
GTFS_ZIP = FIXTURES / "gtfs_min.zip"
STOPS = {324: "Cintorín Slávičie", 254: "Patrónka", 325: "Zoo"}
FILES = {324: "tabs_324_slavicie.jsonl", 254: "tabs_254_patronka.jsonl", 325: "tabs_325_zoo.jsonl"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def events(stop_id: int) -> list[dict]:
    with (FIXTURES / FILES[stop_id]).open() as fh:
        return [json.loads(line) for line in fh]


def recorded_client(stop_id: int, fresh: bool = True):
    """Stand-in for ImhdPushClient fed with the recorded messages.

    Returns (client, time of the last message).
    """
    client = SimpleNamespace(board=Board(), vehicles={}, info=[], fresh=lambda _age: fresh)
    last = None
    for e in events(stop_id):
        if e["event"] == "tabs":
            client.board.update(e["data"])
            last = e["t"]
        elif e["event"] == "vInfo":
            d = e["data"]
            client.vehicles[d["issi"]] = {"type": d["type"], "low_floor": bool(d["lf"]), "ac": bool(d["ac"])}
    return client, last


@pytest.fixture(scope="session")
def index():
    return gtfs.build_index(GTFS_ZIP, {gtfs.normalize(n) for n in STOPS.values()})
