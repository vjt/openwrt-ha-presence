"""Main entrypoint for openwrt-presence."""

from __future__ import annotations

import asyncio
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

    def _on_connect(
        client: mqtt.Client,
        userdata,
        flags,
        reason_code,
        properties=None,
    ) -> None:
        logger.info("mqtt_connected", reason_code=str(reason_code))
        publisher.on_connected()

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

    client.connect_async(config.mqtt.host, config.mqtt.port)
    client.loop_start()

    source = ExporterSource(
        node_urls=config.node_urls,
        tracked_macs={
            mac for person_cfg in config.people.values() for mac in person_cfg.macs
        },
        dns_cache_ttl=config.dns_cache_ttl,
    )

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("shutdown_signal")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    logger.info("initial_query")
    readings = await source.query()
    now = datetime.now(UTC)
    engine.process_snapshot(now, readings)

    # Publish current state for every person regardless of transition.
    # Closes two gaps at once: (a) an already-away person at startup would
    # produce no transition, hence no publish, leaving HA with stale
    # retained state; (b) the audit log would be silent about what we just
    # told HA.  A startup always emits one state_change per person.
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

            now = datetime.now(UTC)
            changes = engine.process_snapshot(now, readings)
            for change in changes:
                publisher.publish_state(change)
    finally:
        await source.close()
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
