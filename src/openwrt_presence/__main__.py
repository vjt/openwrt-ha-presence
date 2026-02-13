"""Main entrypoint for openwrt-presence."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from openwrt_presence.config import Config
from openwrt_presence.engine import PresenceEngine
from openwrt_presence.logging import setup_logging, log_state_change
from openwrt_presence.mqtt import MqttPublisher

logger = logging.getLogger(__name__)


async def _tick_loop(engine: PresenceEngine, publisher: MqttPublisher, interval: float = 30.0) -> None:
    """Periodically check departure/global timers."""
    while True:
        await asyncio.sleep(interval)
        now = datetime.now(timezone.utc)
        changes = engine.tick(now)
        for change in changes:
            publisher.publish_state(change)
            log_state_change(change)


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
    if config.source.type == "victorialogs":
        from openwrt_presence.sources.victorialogs import VictoriaLogsSource
        assert config.source.url is not None
        source = VictoriaLogsSource(config.source.url)

        # Backfill to reconstruct state
        logger.info("Starting backfill from VictoriaLogs")
        async for event in source.backfill():
            changes = engine.process_event(event)
            for change in changes:
                publisher.publish_state(change)
                log_state_change(change)
        logger.info("Backfill complete")

        event_stream = source.tail()

    elif config.source.type == "syslog":
        from openwrt_presence.sources.syslog import SyslogSource
        assert config.source.listen is not None
        source = SyslogSource(config.source.listen)
        event_stream = source.tail()

    else:
        logger.error("Unknown source type: %s", config.source.type)
        sys.exit(1)

    # Start tick loop
    tick_task = asyncio.create_task(_tick_loop(engine, publisher))

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Main event processing loop
    try:
        async for event in event_stream:
            if stop_event.is_set():
                break
            changes = engine.process_event(event)
            for change in changes:
                publisher.publish_state(change)
                log_state_change(change)
    finally:
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            pass
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
