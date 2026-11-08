"""ImhdPushClient lifecycle with a fake socket.io client."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from custom_components.mhd_ba import imhd

from .conftest import events


class FakeSio:
    instances: list[FakeSio] = []
    fail = 0  # number of connects to fail

    def __init__(self, **_kw):
        self.h = {}
        self.emitted = []
        FakeSio.instances.append(self)

    def event(self, fn):
        self.h[fn.__name__] = fn
        return fn

    def on(self, name):
        def deco(fn):
            self.h[name] = fn
            return fn
        return deco

    async def emit(self, name, data=None):
        self.emitted.append((name, data))

    async def connect(self, *_a, **_kw):
        if FakeSio.fail:
            FakeSio.fail -= 1
            raise ConnectionError("boom")
        await self.h["connect"]()
        for e in events(324):
            if e["event"] in self.h:
                await self.h[e["event"]](e["data"])

    async def disconnect(self):
        if "disconnect" in self.h:
            await self.h["disconnect"]()


async def _client(**kw):
    changes = []
    c = imhd.ImhdPushClient(
        324,
        lambda coro, name: asyncio.get_running_loop().create_task(coro, name=name),
        lambda: changes.append(1),
        backoff_min=0.01,
        check_interval=0.01,
        **kw,
    )
    return c, changes


async def test_connect_subscribe_and_receive():
    FakeSio.instances.clear()
    FakeSio.fail = 0
    with patch.object(imhd.socketio, "AsyncClient", FakeSio):
        c, changes = await _client()
        c.start()
        for _ in range(100):
            if c.connected:
                break
            await asyncio.sleep(0.01)
        assert c.connected and changes
        assert ("tabStart", [324, "*"]) in FakeSio.instances[0].emitted
        assert c.board.departures
        assert "1:3503" in c.vehicles
        await c.stop()
        assert not c.connected


async def test_retries_until_connected():
    FakeSio.instances.clear()
    FakeSio.fail = 3
    with patch.object(imhd.socketio, "AsyncClient", FakeSio):
        c, _ = await _client()
        c.start()
        for _ in range(300):
            if c.connected:
                break
            await asyncio.sleep(0.01)
        assert c.connected
        assert len(FakeSio.instances) == 4
        await c.stop()


async def test_reconnect_after_disconnect():
    FakeSio.instances.clear()
    FakeSio.fail = 0
    with patch.object(imhd.socketio, "AsyncClient", FakeSio):
        c, _ = await _client()
        c.start()
        for _ in range(100):
            if c.connected:
                break
            await asyncio.sleep(0.01)
        await FakeSio.instances[0].h["disconnect"]()  # server kicks us
        for _ in range(300):
            if len(FakeSio.instances) > 1 and c.connected:
                break
            await asyncio.sleep(0.01)
        assert len(FakeSio.instances) > 1 and c.connected
        assert c.reconnects >= 1
        await c.stop()
