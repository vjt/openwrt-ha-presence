"""Test doubles. Boundaries only — FakeMqttClient replaces paho,
FakeSource replaces ExporterSource. Never fake the engine or config."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from openwrt_presence.engine import StationReading


@dataclass(frozen=True)
class PublishedMsg:
    topic: str
    payload: str
    qos: int
    retain: bool


class FakeMqttClient:
    """Minimal paho.mqtt.client.Client stand-in.

    Records everything to `published`. LWT in `lwt`. Records
    connect/disconnect. Does NOT simulate the network — callers drive
    `trigger_connect()` / `trigger_disconnect()` to invoke callbacks.
    """

    def __init__(self) -> None:
        self.published: list[PublishedMsg] = []
        self.lwt: PublishedMsg | None = None
        self.connected: bool = False
        self._on_connect: Callable[..., None] | None = None
        self._on_disconnect: Callable[..., None] | None = None
        self.connect_async_called: bool = False
        self.loop_start_called: bool = False
        self.loop_stop_called: bool = False
        self.disconnect_called: bool = False
        self.username: str | None = None
        self.password: str | None = None
        self.reconnect_delay: tuple[int, int] | None = None
        self.max_queued: int | None = None

    # ------------ paho surface ------------

    def username_pw_set(self, username: str, password: str | None) -> None:
        self.username = username
        self.password = password

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def max_queued_messages_set(self, n: int) -> None:
        self.max_queued = n

    def will_set(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> None:
        self.lwt = PublishedMsg(topic=topic, payload=payload, qos=qos, retain=retain)

    def connect_async(self, host: str, port: int) -> None:
        self.connect_async_called = True

    def loop_start(self) -> None:
        self.loop_start_called = True

    def loop_stop(self) -> None:
        self.loop_stop_called = True

    def disconnect(self) -> None:
        self.disconnect_called = True
        self.connected = False

    def publish(
        self, topic: str, payload: str = "", qos: int = 0, retain: bool = False
    ) -> "_FakePublishResult":
        self.published.append(
            PublishedMsg(topic=topic, payload=str(payload), qos=qos, retain=retain)
        )
        return _FakePublishResult(rc=0, mid=len(self.published))

    # paho sets these as attributes; we record them
    @property
    def on_connect(self) -> Callable[..., None] | None:
        return self._on_connect

    @on_connect.setter
    def on_connect(self, cb: Callable[..., None]) -> None:
        self._on_connect = cb

    @property
    def on_disconnect(self) -> Callable[..., None] | None:
        return self._on_disconnect

    @on_disconnect.setter
    def on_disconnect(self, cb: Callable[..., None]) -> None:
        self._on_disconnect = cb

    # ------------ test driver helpers ------------

    def trigger_connect(self, reason_code: Any = 0) -> None:
        """Simulate broker handshake success."""
        self.connected = True
        if self._on_connect is not None:
            self._on_connect(self, None, {}, reason_code, None)

    def trigger_disconnect(self, reason_code: Any = 0) -> None:
        """Simulate broker disconnect."""
        self.connected = False
        if self._on_disconnect is not None:
            self._on_disconnect(self, None, {}, reason_code, None)

    def clear(self) -> None:
        """Drop recorded publishes — use between phases of a test."""
        self.published.clear()


@dataclass
class _FakePublishResult:
    rc: int
    mid: int

    def wait_for_publish(self, timeout: float | None = None) -> None:
        return


@dataclass
class FakeSource:
    """Returns pre-programmed sequences of readings from .query().

    Use .schedule(reading_list, reading_list, ...) to feed multiple
    poll cycles. Raises the exception if raise_on_next is set."""

    readings_queue: list[list[StationReading]] = field(default_factory=list)
    raise_on_next: Exception | None = None
    closed: bool = False

    async def query(self) -> list[StationReading]:
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        if not self.readings_queue:
            return []
        return self.readings_queue.pop(0)

    async def close(self) -> None:
        self.closed = True

    def schedule(self, *batches: list[StationReading]) -> None:
        self.readings_queue.extend(batches)
