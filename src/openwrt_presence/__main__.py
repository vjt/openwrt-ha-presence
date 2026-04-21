"""Main entrypoint for openwrt-presence."""

from __future__ import annotations

import asyncio
import logging as stdlib_logging
import os
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt
import structlog

from openwrt_presence.config import Config
from openwrt_presence.engine import PresenceEngine
from openwrt_presence.logging import setup_logging
from openwrt_presence.mqtt import MqttPublisher
from openwrt_presence.sources.exporters import ExporterSource

if TYPE_CHECKING:
    from openwrt_presence.sources.base import Source

logger = structlog.get_logger()


async def _run(
    config: Config,
    client,  # MqttClient-shaped — FakeMqttClient in tests, paho in prod
    source: Source,
) -> None:
    """Run eve to shutdown with injected broker client and source.

    Separated from main() for testability — tests pass FakeMqttClient and
    FakeSource; production wires real paho + ExporterSource in main().
    """
    publisher = MqttPublisher(config, client)
    # LWT must be registered BEFORE connect_async — paho sends the will to
    # the broker inside the CONNECT packet; setting it after the handshake
    # is a no-op.  Wired here (not in MqttPublisher.__init__) so the
    # ordering constraint is discoverable and testable.  See H1.
    client.will_set(
        publisher.availability_topic,
        publisher.OFFLINE_PAYLOAD,
        qos=1,
        retain=True,
    )
    engine = PresenceEngine(config)

    # Bind asyncio loop before wiring paho callbacks — _on_connect runs
    # on paho's network thread and must hop back here via
    # loop.call_soon_threadsafe to touch publisher state (C2).
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    connected_event = asyncio.Event()

    def _on_connect(
        client,
        userdata,
        flags,
        reason_code,
        properties=None,
    ) -> None:
        logger.info("mqtt_connected", reason_code=str(reason_code))

        def _reseed() -> None:
            try:
                publisher.on_connected()
            except Exception:
                logger.exception("on_connected_failed")

        # Order matters: _reseed (discovery + availability + cached
        # state) must land on the asyncio loop BEFORE connected_event
        # fires, so _run's wait gate only proceeds past a broker that
        # has already seen our discovery packets. FIFO scheduling on
        # call_soon_threadsafe gives us that ordering for free.
        loop.call_soon_threadsafe(_reseed)
        loop.call_soon_threadsafe(connected_event.set)

    def _on_disconnect(
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties=None,
    ) -> None:
        logger.warning("mqtt_disconnected", reason_code=str(reason_code))

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect

    client.enable_logger(stdlib_logging.getLogger("paho.mqtt.client"))

    client.connect_async(config.mqtt.host, config.mqtt.port)
    client.loop_start()

    # Wait for broker handshake before seeding state. on_connected (fired
    # from _on_connect) is what publishes discovery + availability; running
    # the initial query + publish_state before that would leave HA with no
    # device entities to receive the /state payload.  10s timeout matches
    # paho's first reconnect_delay window — if we can't get the broker in
    # 10s we seed anyway (paho queues locally) and let reconnect fix it.
    try:
        await asyncio.wait_for(connected_event.wait(), timeout=10.0)
    except TimeoutError:
        logger.warning("mqtt_connect_timeout_seeding_anyway")

    logger.info("initial_query")
    try:
        readings = await source.query()
    except Exception:
        logger.exception("initial_query_failed")
        readings = []
    now = datetime.now(UTC)
    engine.process_snapshot(now, readings)

    # Publish current state for every person regardless of transition.
    # Closes two gaps at once: (a) an already-away person at startup would
    # produce no transition, hence no publish, leaving HA with stale
    # retained state; (b) the audit log would be silent about what we just
    # told HA.  A startup always emits one state_computed per person.
    for person in config.people:
        snapshot = engine.get_person_snapshot(person, now)
        publisher.publish_state(snapshot)

    logger.info("poll_loop_started", interval=config.poll_interval)

    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.poll_interval)
                break  # stop_event was set
            except TimeoutError:
                pass  # poll interval elapsed, do a cycle

            try:
                readings = await source.query()
            except Exception:
                logger.exception("query_error")
                continue

            if source.all_nodes_unhealthy:
                logger.error(
                    "all_nodes_unreachable",
                    nodes=list(config.node_urls.keys()),
                )
                continue

            now = datetime.now(UTC)
            changes = engine.process_snapshot(now, readings)
            for change in changes:
                publisher.publish_state(change)
    finally:
        await source.close()
        # Shutdown ordering is deliberate (H1): loop_stop() halts paho's
        # network thread FIRST, so the subsequent disconnect() cannot send
        # a DISCONNECT packet. The broker sees a TCP drop and fires our
        # LWT (status=offline) — exactly what we want: HA marks entities
        # unavailable on planned shutdowns. Do NOT reorder without also
        # rethinking the LWT semantics in mqtt.py.
        client.loop_stop()
        client.disconnect()
        logger.info("shutdown_complete")


def main() -> None:
    config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    config = Config.from_yaml(config_path)
    setup_logging()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.max_queued_messages_set(1000)

    source = ExporterSource(
        node_urls=config.node_urls,
        tracked_macs=config.tracked_macs,
        dns_cache_ttl=config.dns_cache_ttl,
    )

    async def _entry() -> None:
        loop = asyncio.get_running_loop()
        # Signal handlers live here, not in _run — keeping signal.add_signal_handler
        # inside _run leaks process-wide handlers into pytest's event loop and
        # breaks test isolation. Tests cancel the _run task directly; production
        # routes SIGTERM/SIGINT to the same task.cancel() path.
        run_task = asyncio.create_task(_run(config, client, source))

        def _shutdown() -> None:
            logger.info("shutdown_signal")
            run_task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown)

        try:
            await run_task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_entry())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
