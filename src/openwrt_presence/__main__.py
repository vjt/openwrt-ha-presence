"""Main entrypoint for openwrt-presence."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from openwrt_presence.config import Config
from openwrt_presence.engine import PresenceEngine
from openwrt_presence.logging import setup_logging, log_state_change
from openwrt_presence.mqtt import MqttPublisher
from openwrt_presence.sources.prometheus import PrometheusSource

logger = logging.getLogger(__name__)


async def _run() -> None:
    config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    config = Config.from_yaml(config_path)

    setup_logging()

    # MQTT setup
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password)

    publisher = MqttPublisher(config, client)
    client.connect(config.mqtt.host, config.mqtt.port)
    client.loop_start()

    publisher.publish_discovery()
    publisher.publish_online()

    engine = PresenceEngine(config)

    # Create source adapter
    assert config.source.url is not None, "source.url is required"
    source = PrometheusSource(config.source.url, config.tracked_macs)

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Initial query to establish current state
    logger.info("Initial query to establish current state")
    readings = await source.query()
    now = datetime.now(timezone.utc)
    changes = engine.process_snapshot(now, readings)
    for change in changes:
        publisher.publish_state(change)
        log_state_change(change)
    logger.info("Initial state established, starting poll loop (interval=%ds)", config.poll_interval)

    # Poll loop
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.poll_interval)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # poll interval elapsed, do a cycle

            try:
                readings = await source.query()
            except Exception:
                logger.exception("Unexpected error during query")
                continue

            now = datetime.now(timezone.utc)
            changes = engine.process_snapshot(now, readings)
            for change in changes:
                publisher.publish_state(change)
                log_state_change(change)
    finally:
        client.loop_stop()
        client.disconnect()
        logger.info("Shutdown complete")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
