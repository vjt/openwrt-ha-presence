"""Main entrypoint for openwrt-presence."""

from __future__ import annotations

import asyncio
import logging as stdlib_logging
import os
import signal
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
import structlog

from openwrt_presence.config import Config
from openwrt_presence.engine import PresenceEngine
from openwrt_presence.logging import setup_logging
from openwrt_presence.mqtt import MqttPublisher
from openwrt_presence.sources.exporters import ExporterSource

logger = structlog.get_logger()


async def _run() -> None:
    config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    config = Config.from_yaml(config_path)

    setup_logging()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.max_queued_messages_set(1000)

    publisher = MqttPublisher(config, client)
    engine = PresenceEngine(config)

    # Bind asyncio loop before wiring paho callbacks — _on_connect runs
    # on paho's network thread and must hop back here via
    # loop.call_soon_threadsafe to touch publisher state (C2).
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_connect(
        client: mqtt.Client,
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

        loop.call_soon_threadsafe(_reseed)

    def _on_disconnect(
        client: mqtt.Client,
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

    source = ExporterSource(
        node_urls=config.node_urls,
        tracked_macs=config.tracked_macs,
        dns_cache_ttl=config.dns_cache_ttl,
    )

    def _signal_handler() -> None:
        logger.info("shutdown_signal")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

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
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
