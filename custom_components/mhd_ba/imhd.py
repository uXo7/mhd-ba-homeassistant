"""imhd.sk realtime departure board over socket.io.

Protocol (the same the imhd.sk web board uses): connect to imhd.sk with
socket.io path `rt/sio2`, emit `tabStart` [stop_id, "*"]; the server then pushes
`tabs` messages, each carrying the departures of some platforms of the stop.

Parsing and connection supervision are adapted from jose1711/ha-imhd (MIT).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import socketio

from .const import IMHD_SOCKETIO_PATH, IMHD_URL, SOURCE_REALTIME, WATCHDOG_SECONDS

_LOGGER = logging.getLogger(__name__)


class ImhdError(Exception):
    """Board could not be fetched."""


@dataclass(slots=True)
class Departure:
    """One departure, from either source."""

    line: str
    destination: str
    time: datetime  # expected (realtime) or scheduled (timetable), aware UTC
    scheduled: datetime | None
    delay: int | None  # minutes; None = no realtime information
    realtime: bool  # vehicle tracked by imhd.sk
    platform: str  # source-specific platform id
    source: str
    platform_label: str | None = None
    vehicle: str | None = None
    stuck: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "destination": self.destination,
            "time": self.time.isoformat(),
            "scheduled": self.scheduled.isoformat() if self.scheduled else None,
            "delay": self.delay,
            "realtime": self.realtime,
            "platform": self.platform_label,  # letter; None when unknown
            "platform_id": self.platform,
            "source": self.source,
            "vehicle": self.vehicle,
            "stuck": self.stuck,
            **self.extra,
        }

    def with_label(self, label: str | None) -> Departure:
        return replace(self, platform_label=label) if label else self


def _ms(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_tabs(data: Any) -> list[Departure]:
    """Parse a `tabs` payload: list of platforms, each with a `tab` list.

    Item fields: `linka` line, `cas` expected time (ms), `casCP` timetable time
    (ms), `casDelta` delay in minutes, `typ` "online" (tracked vehicle) or "cp"
    (timetable only), `issi` vehicle, `cielStr` destination, `uviaznute` stuck.
    """
    result: list[Departure] = []
    if not isinstance(data, list):
        return result
    for platform in data:
        if not isinstance(platform, dict):
            continue
        plat = str(platform.get("nastupiste", ""))
        for item in platform.get("tab") or []:
            if not isinstance(item, dict):
                continue
            line = str(item.get("linka", "")).strip()
            expected = _ms(item.get("cas"))
            if expected is None or not line or line == "►":  # "►" = no line, a marker
                continue
            realtime = item.get("typ", "cp") != "cp"
            scheduled = _ms(item.get("casCP")) or expected
            try:
                delta = int(item.get("casDelta") or 0)
            except (TypeError, ValueError):
                delta = 0
            vehicle = item.get("issi")
            result.append(
                Departure(
                    line=line,
                    destination=str(item.get("cielStr") or item.get("konecnaZstr") or ""),
                    time=expected,
                    scheduled=scheduled,
                    delay=delta if realtime else None,
                    realtime=realtime,
                    platform=plat,
                    source=SOURCE_REALTIME,
                    vehicle=None if vehicle in (None, "", "offline") else str(vehicle),
                    stuck=bool(item.get("uviaznute", False)),
                )
            )
    result.sort(key=lambda d: d.time)
    return result


class Board:
    """Departure board assembled from `tabs` messages.

    A `tabs` message replaces the departures of the platforms it carries;
    platforms not in the message are kept. An empty `tab` = no departures.
    """

    def __init__(self) -> None:
        self._platforms: dict[str, list[Departure]] = {}
        self.messages = 0

    def update(self, data: Any) -> None:
        new: dict[str, list[Departure]] = {}
        for dep in parse_tabs(data):
            new.setdefault(dep.platform, []).append(dep)
        if isinstance(data, list):
            for platform in data:
                if isinstance(platform, dict):
                    new.setdefault(str(platform.get("nastupiste", "")), [])
        self._platforms.update(new)
        self.messages += 1

    @property
    def departures(self) -> list[Departure]:
        out = [d for deps in self._platforms.values() for d in deps]
        out.sort(key=lambda d: d.time)
        return out


def clean_info(data: Any) -> list[Any]:
    """`iText` payload (service notices) without empty entries."""
    items = data if isinstance(data, list) else [data]
    return [i for i in items if i not in (None, "", [], {})]


class ImhdPushClient:
    """Keeps a socket.io connection subscribed to one stop, supervised.

    python-socketio does not reconnect after a server-initiated disconnect, and a
    live connection can silently stop delivering `tabs`, so:

    * any disconnect -> reconnect with exponential backoff, forever;
    * no `tabs` for `watchdog` seconds -> drop and reconnect;
    * a (re)connection counts only once the first `tabs` arrives.

    Unlike the original, start() never fails: the timetable covers the gap and
    the supervisor keeps trying in the background.
    """

    def __init__(
        self,
        stop_id: int,
        create_task: Callable[[Coroutine[Any, Any, Any], str], asyncio.Task],
        on_change: Callable[[], None],
        http_session=None,
        *,
        timeout: float = 15,
        watchdog: float = WATCHDOG_SECONDS,
        check_interval: float = 30,
        backoff_min: float = 2,
        backoff_max: float = 300,
    ) -> None:
        self.stop_id = stop_id
        self._create_task = create_task
        self._on_change = on_change
        self._session = http_session
        self._timeout = timeout
        self._watchdog = watchdog
        self._check_interval = check_interval
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self.board = Board()  # kept across reconnects
        self.info: list[Any] = []
        self.vehicles: dict[str, dict[str, Any]] = {}  # issi -> type, low floor, A/C
        self.connected = False
        self.last_update: datetime | None = None
        self.last_error: str | None = None
        self.reconnects = 0
        self._sio: socketio.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._last_tabs = 0.0
        self._stopping = False

    def fresh(self, max_age: timedelta) -> bool:
        """Connected and the last board is recent enough to trust."""
        return (
            self.connected
            and self.last_update is not None
            and datetime.now(timezone.utc) - self.last_update <= max_age
        )

    def start(self) -> None:
        self._task = self._create_task(self._run(), f"mhd_ba imhd {self.stop_id}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._drop()

    # -- internals --------------------------------------------------------

    async def _connect(self) -> asyncio.Event:
        """Open a connection, subscribe, wait for the first board.

        Returns an event that is set when this connection goes away.
        """
        kwargs: dict[str, Any] = {"reconnection": False}
        if self._session is not None:
            kwargs["http_session"] = self._session
        sio = socketio.AsyncClient(**kwargs)
        gone = asyncio.Event()
        first = asyncio.Event()
        loop = asyncio.get_running_loop()

        @sio.event
        async def connect() -> None:
            # The subscription lives only within a connection.
            await sio.emit("tabStart", [self.stop_id, "*"])
            await sio.emit("infoStart")

        @sio.event
        async def disconnect(*_args) -> None:
            gone.set()

        @sio.on("tabs")
        async def on_tabs(data) -> None:
            self.board.update(data)
            self.last_update = datetime.now(timezone.utc)
            self._last_tabs = loop.time()
            if not first.is_set():
                first.set()
                self.connected = True
            self._on_change()

        @sio.on("iText")
        async def on_itext(data) -> None:
            self.info = clean_info(data)
            self._on_change()

        @sio.on("vInfo")
        async def on_vinfo(data) -> None:
            if isinstance(data, dict) and data.get("issi"):
                self.vehicles[str(data["issi"])] = {
                    "type": data.get("type"),
                    "low_floor": bool(data.get("lf")),
                    "ac": bool(data.get("ac")),
                }

        self._sio = sio
        try:
            await sio.connect(
                IMHD_URL,
                socketio_path=IMHD_SOCKETIO_PATH,
                transports=["websocket", "polling"],
                wait_timeout=self._timeout,
            )
            await asyncio.wait_for(first.wait(), self._timeout)
        except Exception as err:  # noqa: BLE001 - any failure means "try again later"
            await self._drop()
            msg = str(err) or type(err).__name__
            if isinstance(err, TimeoutError):
                msg = f"no data within {self._timeout:g} s"
            self.last_error = msg
            raise ImhdError(msg) from err
        self.last_error = None
        return gone

    async def _drop(self) -> None:
        sio, self._sio = self._sio, None
        if sio is not None:
            try:
                await sio.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self.connected:
            self.connected = False
            self._on_change()

    async def _watch(self, gone: asyncio.Event) -> str:
        """Block while the connection is alive and delivering data."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.wait_for(gone.wait(), self._check_interval)
                return "disconnected"
            except TimeoutError:
                silent = loop.time() - self._last_tabs
                if silent > self._watchdog:
                    return f"no data for {silent:.0f} s"

    async def _run(self) -> None:
        delay = self._backoff_min
        first_attempt = True
        while not self._stopping:
            try:
                gone = await self._connect()
            except ImhdError as err:
                _LOGGER.debug(
                    "imhd.sk stop %s: connect failed (%s), retry in %.0f s",
                    self.stop_id, err, delay,
                )
                if first_attempt:
                    _LOGGER.warning(
                        "imhd.sk stop %s unavailable (%s), using timetable", self.stop_id, err
                    )
                    first_attempt = False
                self._on_change()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._backoff_max)
                continue
            if not first_attempt:
                self.reconnects += 1
                _LOGGER.info("imhd.sk stop %s: connection restored", self.stop_id)
            first_attempt = False
            delay = self._backoff_min
            reason = await self._watch(gone)
            self.last_error = reason
            _LOGGER.info("imhd.sk stop %s: %s, reconnecting", self.stop_id, reason)
            await self._drop()
            await asyncio.sleep(self._backoff_min)
