# openwrt-presence

WiFi-based presence detection for Home Assistant. Scrapes RSSI from
OpenWrt APs running `prometheus-node-exporter-lua`, runs a pure-logic
state machine, publishes per-person home/away + room via MQTT.

This codebase feeds alarm automations. Treat it as security-critical.

## Stack

- Python 3.11+ (uses `from __future__ import annotations`, PEP 604 `X | Y`)
- `asyncio` + `aiohttp` for scraping
- `paho-mqtt` v2 (CallbackAPIVersion.VERSION2)
- `structlog` for JSON logging
- `pyyaml` for config
- `pytest` + `pytest-asyncio` (asyncio_mode = auto)
- Packaged src-layout: `src/openwrt_presence/`

## This is security software

Eve feeds `alarm_control_panel.alarm_arm_away` automations. One wrong
transition can arm the alarm on an occupant or leave the house
unprotected. Every design choice optimises for **correctness over
convenience**:

- **Fail-secure, not fail-safe.** Unknown state = AWAY. Dead AP =
  eventual AWAY *unless the all-nodes-unreachable circuit breaker
  says we're blind*, in which case **hold** — do not arm on
  blindness.
- **Crash loud on unexpected input.** Malformed `/metrics`, bad MAC,
  non-integer RSSI, unknown config section — raise with an
  operator-actionable message. Never `.get(..., default)` on
  required data. Never skip-and-hope.
- **Audit trail non-optional.** `state_computed` + `state_delivered`
  are the forensic record. Don't gate them behind a log-level check.
- **No "smart" behavior.** Pedantic correctness over convenience.
  If the review didn't ask for a fallback or recovery path, don't
  add one.
- **State the contract.** Every public method's docstring names the
  return type and the exceptions it raises. If you can't name the
  exceptions, the method isn't designed.

## Run / dev

```bash
# dev loop
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v

# run locally
CONFIG_PATH=config.yaml python -m openwrt_presence

# prod
docker compose up -d --build

# tail logs pretty
docker container logs <container> -f 2>&1 | openwrt-monitor
```

## Architecture

```
ExporterSource.query()  →  list[StationReading]
         ↓
PresenceEngine.process_snapshot(now, readings)  →  list[StateChange]
         ↓
MqttPublisher.publish_state(change) → log_state_computed + log_state_delivered
```

- `config.py` — YAML parsed into frozen dataclasses at boundary
- `engine.py` — pure logic, no I/O, no `datetime.now()` internally;
  `now` injected from caller. State per MAC: `CONNECTED → DEPARTING → AWAY`.
- `sources/exporters.py` — parallel HTTP scrape of `/metrics`, regex
  parse of `wifi_station_signal_dbm{mac="..."} <rssi>`, DNS + session
  pooling, health tracking (only logs on transition healthy↔unreachable)
- `mqtt.py` — HA MQTT Discovery + retained state + LWT (`status = offline`)
- `mqtt_client.py` — structural `MqttClient` Protocol; minimum paho
  surface typed so `_run` + `FakeMqttClient` share one contract
- `audit.py` — `log_state_computed` / `log_state_delivered` — the
  forensic trail; imported by `mqtt.py` at the publish gate
- `logging.py` — structlog setup boundary → JSON on stderr, ISO `ts`,
  uppercase levels
- `monitor.py` — stdin JSON → ANSI pretty-print (CLI: `openwrt-monitor`).
  Consumes `audit.py` shape via a local `AuditRecord` TypedDict
- `__main__.py` — wires it all together, signal-driven shutdown

### Node types — `exit` matters

- **Exit nodes** (`exit: true`, e.g. garden AP) use `departure_timeout`
  (default 120s). Disappearing from one = person walked out.
- **Interior nodes** use `away_timeout` (default 18h). Safety net for
  phone doze — a bedroom phone vanishing from Wi-Fi for 30 min is still
  "home".
- If **no** exit nodes configured, everything falls back to
  `departure_timeout`. `Config.timeout_for_node(name)` owns this logic —
  don't duplicate it.

## Engineering standards

### Type system
- **Frozen dataclasses, not pydantic.** Consistency with existing code
  beats theoretical purity. Use `@dataclass(frozen=True)` for config /
  value objects, plain `@dataclass` for mutable trackers.
- **Type annotations on every signature.** No exceptions. Use `|` union
  syntax (Python 3.11+).
- **Enums over string constants** for state. `DeviceState` is the model.
- **Parse at the boundary.** YAML → `Config.from_dict` validates and
  raises `ConfigError` with a human-readable message. Inside the code,
  types are load-bearing — don't defensively re-check.
- **No `Any` in public signatures.** `dict[str, Any]` only at the
  YAML-parsing boundary inside `Config.from_dict`.

### Error handling
- **Never swallow exceptions silently.** `__main__._run` catches
  `source.query()` errors and emits `logger.exception("query_error")`
  — the whole point is visibility. Don't copy that pattern elsewhere
  as a rubber stamp.
- **Per-node failure is isolated**, not fatal. One AP unreachable
  should never crash the poll loop. See `ExporterSource.query` —
  health transition logging is deliberate to avoid log spam on a
  flapping AP.
- **No `.get()` fallbacks on required config.** Missing required
  fields must raise `ConfigError`. Optional fields get explicit
  defaults in `Config.from_dict`, not `.get()` inline.
- **Timestamps are UTC, passed explicitly.** The engine never calls
  `datetime.now()`. Callers inject `datetime.now(timezone.utc)`. Tests
  use frozen synthetic timestamps.

### Security-critical behavior (alarm pathway)

This service drives `alarm_control_panel.alarm_arm_away` automations
via HA. Fail modes have physical consequences.

- **Fail-secure on scrape errors.** If an AP is unreachable, the
  engine sees an empty snapshot from it — devices on that AP will
  eventually DEPART. This is the correct behavior (we don't know
  they're there) but means **a dead AP can generate false departures**.
  Keep the `departure_timeout` honest for exit nodes; the 18h
  interior safety net exists specifically to avoid arming the alarm
  when the bedroom AP burps.
- **Never default to "home".** Unknown MAC state = AWAY. Added devices
  must appear via `process_snapshot` to transition to CONNECTED.
- **Retained MQTT is a contract.** Every state topic is published
  `retain=True`. HA restarts must re-receive the last known state.
  Don't break retention without thinking about what HA sees on reboot.
- **LWT must fire.** The MQTT `will_set` on the availability topic is
  how HA marks entities unavailable when the service crashes. Don't remove
  it or change the payload without updating the discovery config too.
- **Log every state change.** `audit.log_state_computed` fires
  unconditionally when the engine decides a transition;
  `audit.log_state_delivered` fires only when all three topic
  publishes return `rc == 0`. A computed without a matching delivered
  is the silent-data-loss tripwire — don't gate either behind a
  log-level check.

### Architecture rules
- **No global state.** Everything constructed in `__main__._run` and
  injected. `PresenceEngine(config)`, `MqttPublisher(config, client)`,
  `ExporterSource(node_urls, tracked_macs, ...)`.
- **Domain types cross module boundaries**, not dicts.
  `StationReading`, `StateChange`, `PersonState` are the vocabulary.
- **One code path per feature.** `Config.timeout_for_node` owns
  exit-vs-interior logic — every caller goes through it.

### Code style
- `from __future__ import annotations` at the top of every module.
- `TYPE_CHECKING` blocks for pure-annotation imports (see engine.py,
  mqtt.py, logging.py).
- Docstrings on public classes / public methods. Private helpers
  (`_foo`) get a one-liner only if the name isn't self-evident.
- MACs normalized lowercase-colon-separated at ingest (`Config._normalize_mac`,
  `_parse_metrics`). Downstream code trusts this — don't re-normalize.
- No comments explaining WHAT — names do that. Only WHY (non-obvious
  invariants, the exit-node safety-net rationale, etc.).

### Testing
- **Fixture-driven config.** Use the `sample_config` fixture in
  `tests/conftest.py`. Don't hand-roll `Config` objects per test.
- **Inject time, never monkeypatch `datetime.now`.** The engine takes
  `now` as a parameter for exactly this reason. Use `_ts(minutes)`
  helpers.
- **Real engine, mocked I/O.** `test_integration.py` drives the engine
  with synthetic readings and asserts on emitted `StateChange`s. MQTT
  client is mocked (it's an I/O boundary). Engine internals are never
  mocked — they're the thing under test.
- **Assert outcomes, not call sequences.** Assert on the `StateChange`
  list, the retained payload, the resulting `PersonState`. Not on
  "was method X called with Y".
- Tests must pass under `pytest -v` before committing. Don't weaken
  assertions to green the bar.

## Config files — what's tracked vs ignored

Gitignored (per-deployment): `config.yaml`, `Dockerfile`,
`docker-compose.yaml`, `*.crt`, `CLAUDE.md`, `.claude/`.

Tracked examples: `config.yaml.example`, `Dockerfile.example`,
`docker-compose.yaml.example`. Update the examples whenever config
schema changes — they are the documentation.

## Quirks
- A custom CA cert may be baked into the container via the `Dockerfile`
  (see `Dockerfile.example` for the pattern). The real `Dockerfile` and
  any `*.crt` files are gitignored.
- OpenWrt APs **must be UTC** — engine is UTC throughout. Clock skew
  between APs and the service can make events arrive out of order; see
  commit `a8f7b6b` for the processing-order tolerance.
- Deployment-specific details (network, container name, fixed IPs)
  live in gitignored `docker-compose.yaml` / `Dockerfile` / `config.yaml`.
  Don't hardcode them into tracked code or docs.

## Deliberate non-decisions (YAGNI)

- **No `Publisher` protocol.** Only `MqttPublisher` exists. A second
  publisher (webhook, secondary broker) would justify introducing
  the protocol; until then, over-abstracting adds ceremony. See
  `docs/reviews/architecture/2026-04-21-architecture-review.md` H10.
- **No runtime config reload.** Adding a person or a MAC requires
  a restart. SIGHUP + diff + in-place mutation is engineering-heavy
  for a 4-person household, and the LWT → HA transient unavailable
  is acceptable operational churn. See finding H14.
- **Monitor → audit-log coupling is typed, not shared.** The
  `openwrt-monitor` CLI consumes the JSON shape of `state_computed`
  / `state_delivered` via a local `AuditRecord` TypedDict in
  `monitor.py` (flat schema, `NotRequired` variant fields). Producer
  and consumer don't share the definition — the TypedDict is
  checkable but purely local to the CLI. Reversed the original
  "accept stringly-typed" non-decision from A1:A7 once the cost of a
  narrow TypedDict turned out lower than the drift risk.
