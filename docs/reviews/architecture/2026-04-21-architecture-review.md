# Architecture Review — 2026-04-21

**Scope:** `/srv/eve/src/openwrt_presence/` (~975 LOC src, ~1300 LOC tests)
**Release at time of review:** `v0.5.0`
**Method:** 4 parallel concern-based agents (boundaries, types, robustness, extensibility+tests). Each read every source file + tests + `CLAUDE.md`.

---

## Executive summary

The core domain logic is genuinely well-factored — `engine.py` is pure, `now`
is injected, domain types cross module boundaries, exit-vs-interior timeout
logic has a single owner. That part of the codebase lives up to the
standards in `CLAUDE.md` and is safe to build on.

Everything around the engine, however, has structural and robustness gaps
that matter specifically because this service feeds alarm automations. The
biggest risks cluster in three areas:

1. **Concurrency & delivery.** The paho network thread races with the
   asyncio loop on shared publisher state, `publish_state` is
   fire-and-forget but the audit log records delivery unconditionally, and
   there is no fail-secure handling when *all* APs go unreachable — the
   exact failure mode that can arm the alarm on a sleeping occupant.
2. **Type discipline at the seams.** `StateChange` permits
   illegal combinations (and the startup path actually produces them,
   publishing empty `mac="", node=""` as **retained** MQTT). Four
   identifier spaces (MAC / person / node / room) are all bare `str`. The
   MQTT boundary is typed `Any`. `Config` is mutable.
3. **Test architecture.** `test_mqtt.py` asserts via `str(mock_call)`
   scans — the opposite of the "assert outcomes, not call sequences" rule
   in `CLAUDE.md`. `__main__._run` has zero test coverage, including the
   startup-seed reconciliation added in `v0.5.0` specifically to close
   alarm-pathway gaps. No end-to-end test proves the dead-AP → departure
   safety claim.

14 CRITICAL+HIGH findings across the four agents deduplicated to 7
CRITICAL and 14 HIGH groups below. Raw per-agent reports are preserved as
appendices for forensic value.

---

## Severity counts (post-dedup)

| Agent | CRITICAL | HIGH | MEDIUM | LOW |
|-------|----------|------|--------|-----|
| A1. Boundaries & responsibilities | 0 | 2 | 4 | 4 |
| A2. Type system & correctness | 2 | 3 | 4 | 4 |
| A3. Robustness & failure modes | 3 | 5 | 6 | 6 |
| A4. Extensibility & tests | 3 | 5 | 5 | 6 |
| **After dedup** | **7** | **14** | **~14** | **~18** |

Several findings appear in 2+ agents (engine/publisher cache duplication,
`StationReading` living in the wrong module, frozen-dataclass discipline,
dead `engine.tick()`). This isn't noise — it's signal: independent lenses
converging on the same structural problems.

---

# CRITICAL findings

## C1. `StateChange` permits illegal combinations, and the startup path produces them

**Source:** A2 §A1+A2, A2 §A13 (redundant)
**Scope:** `engine.py:27-36,179,270-281`, `mqtt.py:97-123`, `__main__.py:85-87`

`StateChange` is one flat dataclass: `home: bool`, `room: str | None`,
`mac: str`, `node: str`, `rssi: int | None`. The invariant "room is None
iff home is False" lives in a comment. The startup path explicitly emits
nonsense for never-seen people: `home=False, room=None, mac="", node="",
rssi=None`. `_emit_state` publishes the empty strings as **retained**
MQTT attributes on a security-critical topic, and `log_state_change`
records the fabricated transition as if it were real.

**Fix:** discriminated union.

```python
@dataclass(frozen=True)
class HomeState:
    person: PersonName; room: Room; mac: Mac; node: NodeName
    timestamp: datetime; rssi: int
    home: Literal[True] = True

@dataclass(frozen=True)
class AwayState:
    person: PersonName; timestamp: datetime
    last_mac: Mac | None = None; last_node: NodeName | None = None
    home: Literal[False] = False

StateChange = HomeState | AwayState
```

Then `get_person_snapshot` either returns `AwayState` (no `""` sentinels)
or `None`, and `_emit_state` pattern-matches; `""` cannot be published.

---

## C2. paho network thread races asyncio loop on `MqttPublisher._last_state`

**Source:** A3 §A1
**Scope:** `mqtt.py:138-149`, `__main__.py:37-42,85-108`

`publisher.on_connected()` runs on paho's `loop_start()` thread
(`_on_connect` callback). It iterates `self._last_state.values()` while
the asyncio poll loop concurrently writes `self._last_state[change.person] = change`
inside `publish_state`. Concurrent mutation with iteration can raise
`RuntimeError: dictionary changed size during iteration` (silently
swallowed by paho — see C3/H3). Retained-state ordering between the
threads can also regress a person from `home` to `not_home` momentarily,
which is the state that arms the alarm.

**Fix:** hand off to asyncio with `loop.call_soon_threadsafe`. Capture the
running loop once at `_run()` start, wrap `publisher.on_connected` in a
`call_soon_threadsafe` from the paho callback. Alternatively, guard
`_last_state` with a `threading.Lock` and snapshot under the lock.
Preferred: the loop-soon path keeps publishing on one thread.

---

## C3. No fail-secure when *all* APs become unreachable

**Source:** A3 §A2
**Scope:** `sources/exporters.py:56-85`, `engine.py:73-135`, `__main__.py:99-108`

`ExporterSource.query()` correctly isolates per-node failures, but when
every AP fails simultaneously (network partition, DNS outage, Docker host
losing the AP VLAN), it returns an empty reading list. The engine cannot
distinguish "nobody home" from "blind": all CONNECTED devices start
DEPARTING, and any whose last representative was on an **exit** node
become AWAY after `departure_timeout` (default 120s). Within two minutes
of total scrape failure the alarm arms on occupants sitting in the
garden. `CLAUDE.md` explicitly flags this but no mitigation lives in
code.

**Fix:** circuit breaker at the source or main-loop level — if every
node's health is currently `False` (and there was at least one prior
healthy reading), skip engine processing and emit
`logger.error("all_nodes_unreachable")`. Resume when any node recovers.
Optionally freeze departure deadlines for devices last seen on a
currently-unhealthy node.

---

## C4. `publish_state` is fire-and-forget, but writes the audit log unconditionally

**Source:** A3 §A3 (overlaps A3 §A10)
**Scope:** `mqtt.py:85-123`, `__main__.py:32`

`self._client.publish(...)` returns an `MQTTMessageInfo` that the code
never inspects. `log_state_change(change)` is called immediately
afterwards, asserting "we told HA". With `max_queued_messages_set(1000)`
and ~3 publishes per transition + startup seed (3×N), the in-memory
queue can overflow within minutes of broker downtime — paho silently
drops the message, `publish()` returns `MQTT_ERR_QUEUE_SIZE`, the audit
log claims delivery anyway. For a security service, an audit log that
*lies* about what HA received is worse than a silent gap.

**Fix:** check `info.rc`. On non-success, emit
`logger.error("publish_failed", topic=..., rc=...)`. Split the log
schema: `state_computed` (engine emitted a change) vs `state_delivered`
(publish succeeded). Optionally raise the queue cap. Consider a clean
`status=offline` retained publish before shutdown.

---

## C5. No `Source` abstraction; `sources/` depends on `engine.py`

**Source:** A4 §A1, A1 §A3 (overlaps), A4 §A10 (overlaps)
**Scope:** `sources/exporters.py:11`, `sources/__init__.py`, `__main__.py:55-63`

`ExporterSource` is a concrete class with no `Protocol`, ABC, or doc'd
contract. `StationReading` — the source→engine contract — lives **in
`engine.py`**, so `sources/exporters.py` imports from engine (reverse
layering). The plural `sources/` subpackage hints at pluggability that
the code doesn't actually provide. Adding a second source (MQTT ingest,
a vendor with different metric names, a composite with redundant data
sources) means editing `__main__._run`, not dropping in a file.

**Fix:** move `StationReading` (and `StateChange`, `PersonState`) to
`domain.py`. Add `sources/base.py`:

```python
class Source(Protocol):
    async def query(self) -> list[StationReading]: ...
    async def close(self) -> None: ...
```

`engine`, `sources/*`, `mqtt`, `logging` all import from `domain`.
`__main__` takes `Source`; a future `CompositeSource` or `FakeSource`
becomes trivial (and enables C7 tests).

---

## C6. Test architecture asserts on mock-call strings, not outcomes

**Source:** A4 §A2
**Scope:** `tests/test_mqtt.py` (entire file), `tests/test_source_exporters.py:93-163`

`CLAUDE.md` §Testing: *"Assert outcomes, not call sequences. Not on 'was
method X called with Y'."* Every test in `test_mqtt.py` stringifies
`MagicMock` calls:

- `[c for c in calls if "device_tracker/alice_wifi/config" in str(c)]`
- LWT check: `assert "status" in str(args) and "offline" in str(args)`
- Retention check: triple-fallback `c[1].get("retain", False) or (len(c[0]) > 2 and c[0][2]) or c[1].get("retain") is True`

`test_source_exporters.py` monkey-patches `source._get_session` and
`source._scrape_ap` with `AsyncMock` and asserts on log output —
testing the mock wiring, not the source.

Real bugs (wrong topic, wrong payload, missing retain on the attributes
topic) can slip through undetected. The retention invariant — *the*
security-critical MQTT contract — is guarded only by stringly-typed
heuristics.

**Fix:** introduce a `FakeMqttClient` that records
`PublishedMsg(topic, payload, qos, retain)` + `.lwt`. Tests assert on
concrete outcomes:

```python
assert PublishedMsg(topic="openwrt-presence/alice/state",
                    payload="home", qos=1, retain=True) in client.published
```

For source tests, use `aiohttp`'s test server or request-level stubbing
instead of patching private methods. `test_engine.py` / `test_integration.py`
follow the correct pattern and are the template to mimic.

---

## C7. `__main__._run` has zero test coverage — security-critical wiring unverified

**Source:** A4 §A3 (overlaps A4 §A12)
**Scope:** `__main__.py` (all 125 lines)

No test exercises:
- the startup-seed loop (lines 85-87) — added in commits `2d4ec23` +
  `5605fc6` specifically to fix alarm-pathway stale-retained-state bugs
- the `on_connected` reconnect-republish wiring
- signal-handler installation and shutdown ordering
- the loop's `except Exception: logger.exception("query_error")`
  isolation (which `CLAUDE.md` explicitly flags as *deliberate* and not
  to be copy-pasted elsewhere)
- startup with an unreachable broker
- end-to-end dead-AP → departure via `ExporterSource` + engine + publisher

**Fix:** add `tests/test_main.py`. Use a `FakeMqttClient` (from C6) and
`FakeSource` (from C5) to drive `_run()` with an injected
`asyncio.Event`. Assert: (1) every person produces exactly one
`state_change` log at startup, (2) a raised query exception doesn't
exit the loop, (3) reconnect triggers republish, (4) dead-AP sequence
produces departure only after `away_timeout`. <150 LOC with the fakes in
place.

---

# HIGH findings

## H1. `MqttPublisher.__init__` silently calls `will_set` — LWT ordering implicit

**Source:** A1 §A1 (overlaps A3 §A4)
**Scope:** `mqtt.py:27-38`, `__main__.py:28-52`

Constructing `MqttPublisher` mutates the injected paho client by calling
`will_set`. Creates a hidden "must construct before `connect_async`"
invariant that's untyped, undocumented, and unchecked. A reasonable
refactor (lazy publisher, test fixture construction after connect)
silently kills LWT — which `CLAUDE.md` flags as security-critical. Also
related: `__main__.py:109-113` calls `loop_stop()` **before**
`disconnect()`, so the clean DISCONNECT packet is never sent and the
broker sees a TCP drop — LWT fires on planned shutdown. May be
intentional but undocumented.

**Fix:** pull `will_set` out of the constructor into `__main__` next to
the other pre-connect client setup. Publisher takes `client` as a pure
collaborator. Separately, decide and **document** shutdown intent:
either (a) publish explicit `status=offline` before `loop_stop` +
`disconnect`, or (b) keep current ordering with a comment explaining
that LWT-on-shutdown is deliberate.

## H2. `MqttPublisher.publish_state` owns audit logging — transport couples to audit trail

**Source:** A1 §A2
**Scope:** `mqtt.py:85-95`, `logging.py:46-57`

`publish_state` (a) caches the change, (b) publishes MQTT, (c) calls
`log_state_change`. Comment justifies the coupling, but encoding it in
the MQTT transport is wrong — `logging.py` now imports `StateChange` (a
domain type) and the audit log becomes a side-effect of MQTT. A second
sink (webhook, Prometheus) would double-log or lose the audit entry. A
test that mocks the publisher also loses the audit line.

**Fix:** introduce a `PresenceSink` facade (or keep in `__main__`) that
is the single callsite for "a state change happened" — emits audit log +
delegates to all publishers. Move `log_state_change` to `engine.py`
(next to `StateChange`) or a new `audit.py`. `logging.py` keeps only
`setup_logging()`.

## H3. `Mac`, `PersonName`, `NodeName`, `Room` all typed as bare `str`

**Source:** A2 §A3
**Scope:** all modules

The whole app is about matching MACs to people on nodes placing them in
rooms. All four are `str`. Evidence:
- `ExporterSource.__init__` defensively re-lowercases `tracked_macs` even
  though `Config._normalize_mac` already ran (`exporters.py:29`)
- `process_snapshot` does `r.mac.lower()` on line 86 even though the
  comment says "already normalized"
- `get_person_snapshot("aa:bb:cc:dd:ee:01", now)` type-checks but
  returns nonsense
- `_best_representative` returns `tuple[str, str, int | None]` — the
  reader has to trust a docstring to know it's `(Mac, NodeName, rssi)`

One refactor away from a silent alarm-pathway bug.

**Fix:** `NewType` on all four at the config boundary. `_normalize_mac`
returns `Mac`; `StationReading.mac: Mac`; downstream code doesn't
re-normalize. Mix-ups become compile errors.

## H4. `Config` is `@dataclass` (mutable); `_mac_lookup` is a leaky semi-private field

**Source:** A2 §A4 (overlaps A1 §A4)
**Scope:** `config.py:35-48,110-148`

Sub-configs are `frozen=True` but the root `Config` isn't. Nothing
mutates it today; `CLAUDE.md` explicitly says frozen for config/value
objects. `_mac_lookup` is a public dataclass field (settable
positionally, mutable `dict` inside); `config._mac_lookup[mac] = "alice"`
would silently reroute a MAC. Also `__main__` rebuilds
`tracked_macs` by hand from `config.people.values()` instead of using
`_mac_lookup` — two paths for the same data.

**Fix:** `@dataclass(frozen=True)` on `Config`. Set `_mac_lookup` via
`object.__setattr__` in `from_dict` with `field(init=False, repr=False)`.
Expose `Config.tracked_macs: frozenset[Mac]` as a property — `__main__`
uses that.

## H5. MQTT boundary typed `Any`

**Source:** A2 §A5
**Scope:** `mqtt.py:27`, `__main__.py:37-50`

`MqttPublisher.__init__(..., client: Any)`. `_on_connect` / `_on_disconnect`
callbacks have untyped `userdata`, `flags`, `reason_code`, `properties`.
`CLAUDE.md`: "No `Any` in public signatures". paho-mqtt 2.x ships type
hints; `CallbackAPIVersion.VERSION2` exists specifically to pin callback
shapes. `_device_block` returns `dict[str, Any]` — tolerable but
cascades.

**Fix:** `client: paho.mqtt.client.Client`. Type callbacks. `_device_block() -> HaDeviceBlock` as a `TypedDict`.

## H6. Startup publishes state before MQTT connects

**Source:** A3 §A5 (overlaps A3 §A10)
**Scope:** `__main__.py:52-88`

`client.connect_async()` + `loop_start()` at line 52-53. Startup seed
publishes (line 85-87) runs immediately — TCP may not be up, `on_connect`
has not fired yet, so discovery + availability haven't been published,
and `_last_state` is being populated while (once `on_connect` fires)
`on_connected()` will iterate it. HA may flap "unavailable → home →
unavailable → home". Startup audit log claims delivery that hasn't
happened.

**Fix:** gate the startup seed on `on_connected` having fired — expose
an `asyncio.Event` from the paho callback via `call_soon_threadsafe`,
`await` it before seeding. Alternative: explicitly call
`publisher.publish_discovery()` + `publish_online()` at the top, then
seed.

## H7. `_on_connect` callback has no try/except — paho swallows exceptions

**Source:** A3 §A6
**Scope:** `__main__.py:37-42`, `mqtt.py:138-149`

paho discards exceptions from user callbacks. If `publisher.on_connected()`
raises (C2 race, serialisation error, any TypeError), paho logs to its
internal logger (not wired to structlog) and moves on. The reconnect
republish — the recovery path after Mosquitto upgrades drop retained
state — silently fails.

**Fix:** wrap `publisher.on_connected()` in try/except that does
`logger.exception("on_connected_failed")`. Also
`client.enable_logger(...)` to pipe paho's internal logs through
structlog.

## H8. `asyncio.get_event_loop()` deprecated — breaks on Python 3.14+

**Source:** A3 §A7
**Scope:** `__main__.py:65`

Inside a coroutine running under `asyncio.run()`, `get_event_loop()` is
deprecated since 3.12 and will raise in 3.14. `pyproject.toml` has no
upper bound on `requires-python`.

**Fix:** `loop = asyncio.get_running_loop()`.

## H9. Initial `source.query()` at startup has no error handling

**Source:** A3 §A8
**Scope:** `__main__.py:75-78`

The poll loop wraps `query()` in try/except. The initial startup query
doesn't. Any unhandled exception (DNS, SessionConfigurationError)
terminates the service before `loop_start()` has established MQTT,
producing a Docker crash-loop. The availability topic never transitions
to `online`.

**Fix:** mirror the poll loop — wrap in
`try: ... except Exception: logger.exception("initial_query_failed")`
and continue into the loop.

## H10. No `Publisher` abstraction — HA MQTT is hardwired

**Source:** A4 §A4
**Scope:** `mqtt.py:16-149`, `__main__.py:34`

`MqttPublisher` is concrete; HA discovery topic structure is baked into
private methods. Adding a webhook publisher, a secondary broker, or a
non-HA consumer requires forking the class.

**Fix:** split into (a) `Publisher` protocol (`publish_state`,
`on_connected`), (b) `HomeAssistantDiscovery` strategy owning topic +
payload schemas, (c) `MqttPublisher` composing (a)+(b)+paho. Enables
`CompositePublisher` without touching `__main__`.

## H11. Two sources of truth for "last per-person state"

**Source:** A1 §A6 (overlaps A4 §A5)
**Scope:** `engine.py:63,268` (`_last_person_state`), `mqtt.py:31,93` (`_last_state`)

Engine caches `PersonState` for change detection. Publisher caches
`StateChange` for reconnect replay. Same underlying fact, two caches.
Also, startup-seed and reconnect-replay use *different* paths: startup
calls `engine.get_person_snapshot`, reconnect reads `publisher._last_state`.
On a long reconnect window, publisher's cache serves stale
`mac`/`node`/`rssi` attributes.

**Fix:** delete `MqttPublisher._last_state`. On reconnect, iterate
`config.people` and call `engine.get_person_snapshot(person, now)` — the
pattern `__main__._run` already uses at startup. Engine is the single
truth.

## H12. `test_integration.py` hand-rolls `Config` despite `sample_config` fixture

**Source:** A4 §A6
**Scope:** `tests/test_integration.py:19-39`, `tests/test_config.py:5-14`

`CLAUDE.md` §Testing: *"Fixture-driven config. Don't hand-roll Config
objects per test."* Integration file has its own `_make_config()` with
different nodes and people than the fixture. `test_config.py` has a
third factory (defensible — it tests validation boundaries — but signals
drift).

**Fix:** expand `sample_config` to cover integration scenarios, or add
a `config_with_nodes` fixture factory. Integration tests reuse.

## H13. Config defaults duplicated between field default and `from_dict`

**Source:** A4 §A7
**Scope:** `config.py:41-44,127-136`

`away_timeout: int = 64800`, `poll_interval: int = 30`,
`exporter_port: int = 9100`, `dns_cache_ttl: int = 300` each appear
*twice* — dataclass field default and `data.get("key", <default>)`
fallback. The field default is effectively dead because `from_dict` is
the documented construction path. `departure_timeout` is required (no
default) — inconsistent. Constructing `Config(...)` directly vs via
`from_yaml` could diverge silently.

**Fix:** module-level constants (`DEFAULT_AWAY_TIMEOUT_SEC = 64800`)
used in both places. Or remove field defaults and require `from_dict`.
Or drop `.get(...)` defaults and pass only present keys. Centralize.

## H14. No runtime config reload — add-a-person requires restart

**Source:** A4 §A8
**Scope:** `__main__.py:23-24`

Config is read once. Adding a new device MAC, a new person, or changing
`away_timeout` because a phone keeps dozing requires `docker restart` →
LWT fires → HA transient unavailable → alarm automations see a flap.
That's the opposite of what the system exists to provide.

**Fix:** either document "restart required" explicitly (simplest), or
wire SIGHUP to reparse + diff + mutate `engine._devices`,
`publisher._topic_prefix`, `source._tracked_macs`, adding/removing from
`_last_person_state` as needed. Feasible but non-trivial.

---

# MEDIUM findings (selected — see appendices for full text)

- **M1. `StationReading` defined in `engine.py`, produced by sources** — move to `domain.py` (A1 §A3, A4 §A10)
- **M2. `__main__` flattens `config.people[*].macs` by hand** — expose `Config.tracked_macs` (A1 §A4)
- **M3. `PresenceEngine.tick()` is dead code in production, alive in tests** — overlapping expiry path (A1 §A5, A4 §A17)
- **M4. `_DeviceTracker` invariant "DEPARTING ⇔ deadline set" lives in logic** — tagged union or test-only assertion (A2 §A6)
- **M5. `StationReading` / `PersonState` not frozen** — CLAUDE.md says value objects should be (A2 §A7)
- **M6. `data.get("mqtt", {})` on required section → `KeyError` not `ConfigError`** — mirror `nodes`/`people` style (A2 §A8)
- **M7. `_compute_person_state` silently returns "away" for unknown person** — dead defensive branch (A2 §A9)
- **M8. No Docker `HEALTHCHECK` — hung process not restarted** — touch `/tmp/alive` each poll (A3 §A9)
- **M9. `client.disconnect()` after `loop_stop()` prevents clean offline publish** — see H1 (A3 §A11)
- **M10. Per-node health state lost across restart** — emit `initial_node_state` after first query (A3 §A12)
- **M11. No response-size cap on `/metrics` scrape** — `response.content.read(1<<20)` (A3 §A13)
- **M12. `response.status` never checked — 503 looks like "no devices"** — `raise_for_status()` (A3 §A14)
- **M13. Hardcoded `_QOS`, retain, topic, HA identifiers, no TLS** — config knobs (A4 §A9)
- **M14. State-machine semantics hardcoded inline (`state in (CONNECTED, DEPARTING)`)** — `DeviceState.is_present` / `contributes_to_room` (A4 §A11)
- **M15. No metrics/health endpoint** — aiohttp sidecar on optional port (A4 §A13)

---

# LOW findings (selected — see appendices)

A1 §A7 (monitor↔log stringly-typed schema), A1 §A8 (no Source
Protocol — subsumed by C5), A1 §A9 (Config not frozen — subsumed by H4),
A1 §A10 (`on_connected` name collides with paho's `on_connect`),
A2 §A10 (`NodeConfig.url` resolution), A2 §A11 (`monitor.py` consumes
raw dict), A2 §A12 (`PersonConfig.macs: list[str]` mutable inside
frozen), A2 §A13 (`home` field redundant with `room is not None` —
subsumed by C1), A3 §A15 (shutdown latency on in-flight query), A3 §A16
(ConfigError before structlog configured), A3 §A17 (task management —
correct, informational), A3 §A18 (KeyboardInterrupt swallow in `main()`),
A3 §A19 (`_devices` bounded — informational), A3 §A20 (`rssi=null` in
HA attributes JSON), A4 §A14 (magic `-100`/`-200` RSSI sentinels),
A4 §A15 (unused `call` import in test_mqtt), A4 §A16 (`monitor.py`
untested), A4 §A19 (`departure_timeout` asymmetric — required but bare
subscript KeyError).

---

# Things genuinely well-done (don't regress)

All four agents independently praised:

- **`engine.py` as pure logic.** No I/O, no `datetime.now()`, `now`
  injected at every entry point. Test shape (`_ts(minutes)`) exploits
  it cleanly.
- **`Config.timeout_for_node`** — single source of truth for the
  exit-vs-interior split. No duplication found.
- **`dict[str, Any]` confined to `Config.from_dict`** — the boundary
  discipline promised in `CLAUDE.md` is actually honoured.
- **`DeviceState` as real enum**, not string constants.
- **`TYPE_CHECKING` blocks** used correctly to avoid runtime cycles.
- **`ExporterSource` lifecycle ownership** — owns its session/connector,
  health tracking, transition-only logging.
- **No global state, no circular imports, no module-level mutable
  singletons.**
- **Fail-secure defaults** — unknown MAC → no effect, unknown person →
  away, empty snapshot → eventual DEPARTING. Never-default-to-home rule
  upheld.
- **Engine tests (`test_engine.py`, `test_integration.py`)** — these
  *do* follow `CLAUDE.md`: real engine, injected time, assertions on
  `StateChange` lists and `PersonState`. The template C6's rewrite
  should mimic.
- **`_node_healthy` transition-only logging** — operational sanity
  without log spam.

---

# Recommended order of attack

Cheapest high-impact fixes first. Each block is roughly a session of
work.

### Session 1 — alarm-pathway safety (must land before next release)

1. **C2** — fix paho thread race (`call_soon_threadsafe`)
2. **C3** — all-APs-unreachable circuit breaker
3. **C4** — check `info.rc`, split log schema
4. **H7** — wrap `_on_connect` body in try/except + `enable_logger`
5. **H8** — `get_running_loop()` (trivial)
6. **H9** — wrap initial query in try/except

### Session 2 — test architecture (enables everything else)

7. **C6** — `FakeMqttClient` + rewrite `test_mqtt.py`
8. **C7** — `tests/test_main.py` driving `_run()` with fakes
9. **H12** — expand `sample_config`, drop integration `_make_config`

### Session 3 — type & domain cleanup

10. **C1** — `StateChange` discriminated union
11. **H3** — `Mac` / `PersonName` / `NodeName` `NewType`s
12. **H4** — freeze `Config`, hide `_mac_lookup`, expose
    `tracked_macs`
13. **H5** — type the MQTT boundary

### Session 4 — structural

14. **C5** — `domain.py` + `Source` protocol
15. **H1** — move `will_set` out of constructor; document shutdown
16. **H2** — audit sink facade
17. **H6** — gate startup on `on_connected`
18. **H10** — `Publisher` protocol + `HomeAssistantDiscovery` strategy
19. **H11** — drop `publisher._last_state`, reconnect via engine
20. **H13** — centralize defaults
21. **M3** — delete `engine.tick()`

### Session 5 — operational

22. **M8** — Docker healthcheck
23. **M11 + M12** — size cap + status check on scrape
24. **H14** — decide & document runtime reload strategy

Everything else is tech-debt backlog and can land opportunistically.

---

# Appendix A — Raw agent reports

Full per-agent outputs preserved for forensic value. Each below is
verbatim from the dispatched agent, before dedup.

## Appendix A.1 — Boundaries & responsibilities

See `_agent_a1_boundaries.md` (kept alongside this file for reference).

## Appendix A.2 — Type system & correctness

See `_agent_a2_types.md`.

## Appendix A.3 — Robustness & failure modes

See `_agent_a3_robustness.md`.

## Appendix A.4 — Extensibility & test architecture

See `_agent_a4_extensibility_tests.md`.
