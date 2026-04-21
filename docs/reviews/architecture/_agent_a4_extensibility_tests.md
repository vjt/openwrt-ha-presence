# Architecture review — extensibility & test architecture

## Summary

The core domain (`engine.py`) is genuinely well-factored: pure logic, time injected,
domain types (`StationReading`, `StateChange`, `PersonState`) crossing module
boundaries, and `Config.timeout_for_node` concentrating the exit/interior split.
That part earns its keep and will absorb future state-machine changes cleanly.
The rest of the system, however, leaks concretions upward: there is no `Source`
or `Publisher` abstraction, HA-specific topic shapes are baked into
`MqttPublisher`, and `ExporterSource` depends on `engine` domain types (reverse
layering). The biggest liability is **test architecture**: `test_mqtt.py` and
`test_source_exporters.py` systematically violate the CLAUDE.md rules they were
written under — asserting on `MagicMock` call bags via `str(call)` scans,
monkey-patching private methods, and checking retention by stringifying call
objects rather than asserting on outcomes. `test_integration.py` hand-rolls a
second `Config` instead of reusing `sample_config`. Most damning: the
security-critical code path (`__main__._run`, including the startup-seed
reconciliation) has **zero test coverage**, and nothing exercises a dead-AP →
departure flow end-to-end through a failing source.

---

## CRITICAL

### A1. No `Source` abstraction — adding a second source type requires surgery
**Concern:** extensibility
**Scope:** `src/openwrt_presence/sources/exporters.py`, `src/openwrt_presence/__main__.py:55-63`
**Problem:** `ExporterSource` is a concrete class with no `Protocol`, ABC, or
even a documented duck-type contract. `__main__._run` instantiates it directly.
There is no `sources/base.py` declaring what a source must do. The
`sources/` package has a lone `__init__.py` with no exports; the package layout
hints at pluggability that isn't there. Worse, `ExporterSource.query` returns
`list[StationReading]` — a type *imported from `engine.py`* — so sources depend
on engine domain types (reverse of the natural layering where the engine
consumes source-produced data).
**Impact:** Adding an MQTT-based source (e.g. consuming a router's native
presence topic), a second AP vendor with a different metric name, or running
two sources in parallel (Prometheus + direct WebSocket) requires editing
`__main__._run`, not just adding a file. The engine cannot be composed with a
fake source in tests without re-declaring the return-type contract. A future
`CompositeSource([exporter, mqtt])` has nowhere to live.
**Recommendation:** Define `sources/base.py`:
```python
class Source(Protocol):
    async def query(self) -> list[StationReading]: ...
    async def close(self) -> None: ...
```
Move `StationReading` to a `domain.py` (or `types.py`) module that both
`engine.py` and `sources/*` import — inverting the dependency so engine
and sources are peers under a shared vocabulary. `__main__` then takes
`Source` (protocol) and can build a list/composite.
**Severity:** CRITICAL

### A2. Test architecture violates its own rules — MQTT and source tests assert on mock calls, not outcomes
**Concern:** test architecture
**Scope:** `tests/test_mqtt.py` (entire file), `tests/test_source_exporters.py:93-163`
**Problem:** CLAUDE.md §Testing explicitly says: *"Assert outcomes, not call
sequences. Assert on the StateChange list, the retained payload, the resulting
PersonState. Not on 'was method X called with Y'."* Every test in
`test_mqtt.py` does exactly the forbidden thing, and does it via the most
brittle possible mechanism — **stringifying the MagicMock call object**:
- `test_mqtt.py:21` — `[c for c in calls if "device_tracker/alice_wifi/config" in str(c)]`
- `test_mqtt.py:35`, `:47`, `:69`, `:79`, `:91`, `:101`, `:111`, `:133`, `:149` — same pattern
- `test_mqtt.py:57` — retention check reads `c[1].get("retain", False) or (len(c[0]) > 2 and c[0][2]) or c[1].get("retain") is True`, a triple-fallback that passes if ANY of three structurally-different call shapes is retained. That's not a test, it's a best-effort smoke.
- `test_mqtt.py:133-136` — LWT test literally `assert "status" in str(args)` / `assert "offline" in str(args)`.

`test_source_exporters.py:106-107,121-122,136-137,153-154` monkey-patches
`source._get_session` and `source._scrape_ap` with `AsyncMock`, then asserts on
log output. That's mocking internals the CLAUDE.md rules say to leave alone —
the thing under test becomes the mock wiring, not the source behavior.

**Impact:** Any refactor that shifts how `client.publish` is called
positionally vs. by keyword will silently pass broken tests or fail-green
working code. The retention invariant — *the* security-critical MQTT contract
— is guarded only by a stringly-typed fallback. Real bugs (wrong topic, wrong
payload, missing retain flag on attributes topic) can slip through because the
assertions don't pin the shape.

**Recommendation:** Introduce a `FakeMqttClient` with a simple recorded
interface:
```python
@dataclass
class PublishedMsg:
    topic: str
    payload: str
    qos: int
    retain: bool
class FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[PublishedMsg] = []
        self.lwt: PublishedMsg | None = None
    def publish(self, topic, payload, qos=0, retain=False): ...
    def will_set(self, topic, payload, qos=0, retain=False): ...
```
Now tests assert on concrete outcomes: `assert PublishedMsg(topic="openwrt-presence/alice/state", payload="home", qos=1, retain=True) in client.published`. For the source tests, spin an `aiohttp` test server (aiohttp ships one) or use `aiohttp`-level request mocking instead of patching private methods.
**Severity:** CRITICAL

### A3. `__main__._run` has zero test coverage — security-critical wiring is unverified
**Concern:** test architecture
**Scope:** `src/openwrt_presence/__main__.py` (all 125 lines)
**Problem:** No test exercises:
- the startup seed loop (`__main__.py:85-87`) which per the comment "closes two gaps" — a stale-retained-state gap AND an audit-log gap. Both are alarm-pathway concerns.
- the `on_connected` callback wiring (lines 37-42) — whether reconnect actually triggers republish.
- signal handler installation and shutdown ordering (lines 72-73, 109-113).
- the `try/except Exception: logger.exception("query_error"); continue` loop-isolation in lines 99-103 — per CLAUDE.md this is a *deliberate* error-handling shape.
- `client.connect_async` failure (what if the broker is unreachable at startup?).
**Impact:** The seeding code was added to fix two concrete bugs (see commits
`2d4ec23`, `5605fc6`). A refactor that reorders calls, drops the initial
`engine.process_snapshot`, or stops publishing for already-away people at
startup will not be caught by any test. An unreachable broker at startup is
invisible to the test suite. The alarm-critical claim "a dead AP produces false
departures but eventually goes AWAY" (CLAUDE.md §Security-critical) is not
tested end-to-end through `_run`.
**Recommendation:** Add `tests/test_main.py` that uses a `FakeMqttClient` and
a fake `Source` to drive `_run()` for one or two iterations with injected
`asyncio.Event`. Assertions: (1) every person produces exactly one startup
state-change log line, (2) simulated query exception doesn't exit the loop,
(3) reconnect callback invokes `publisher.on_connected`, (4) signal sets
`stop_event`. The fakes make this tractable in <100 LOC.
**Severity:** CRITICAL

---

## HIGH

### A4. No `Publisher` abstraction — HA MQTT is hardwired
**Concern:** extensibility
**Scope:** `src/openwrt_presence/mqtt.py:16-149`, `src/openwrt_presence/__main__.py:34`
**Problem:** `MqttPublisher` is concrete. The HA MQTT Discovery topic
structure (`homeassistant/device_tracker/{person}_wifi/config`) is baked into
two private methods. Topic prefix is configurable; nothing else is. Adding
a webhook publisher, a secondary MQTT broker (for redundancy), or non-HA
consumers requires a fork of the class. `__main__._run` takes `MqttPublisher`
by name, not by protocol.
**Impact:** The caller pattern `publisher.publish_state(change)` is already a
decent abstraction — but there's no interface declaring it. A future "publish
to HA + log to remote audit store" composition has no natural insertion point.
If HA ever changes its discovery schema (they have before — `expire_after`,
`icon_template`, etc.), the discovery methods need editing with no schema
versioning.
**Recommendation:** Split `MqttPublisher` into (a) `Publisher` protocol
(`publish_state`, `on_connected`), (b) `HomeAssistantDiscovery` strategy
object that owns topic shapes and payload schemas, (c) `MqttPublisher` that
composes a discovery strategy with a paho client. Enables a future
`CompositePublisher([mqtt, webhook])` without touching `__main__`.
**Severity:** HIGH

### A5. Two sources of truth for "last person state"
**Concern:** maintainability
**Scope:** `src/openwrt_presence/engine.py:63,268` (`_last_person_state`),
`src/openwrt_presence/mqtt.py:31,93` (`_last_state`)
**Problem:** `PresenceEngine._last_person_state: dict[str, PersonState]` is
the engine's audit of "what did I last emit". `MqttPublisher._last_state:
dict[str, StateChange]` is the publisher's cache for reconnect replay. These
two caches are populated from the same events, in the same order, for
overlapping reasons. The publisher cache exists because on reconnect we want
to republish state *including* rssi/mac/node attributes — which the engine
could produce via `get_person_snapshot` if called with the current time.
**Impact:** If someone adds a new field to `StateChange`, they must update
both caches' behavior mentally. A bug where the publisher cache diverges from
engine truth (e.g. publisher crash between cache update and actual publish) is
possible and invisible. Reconnect republish and startup seed use *different*
code paths (startup uses `engine.get_person_snapshot`, reconnect replays
`_last_state`) — two ways to accomplish the same thing.
**Recommendation:** Drop `MqttPublisher._last_state`. On `on_connected`,
call back into the engine: `for person in config.people: publisher._emit_state(engine.get_person_snapshot(person, now))`. Requires
passing engine (or a `StateProvider` callback) into `MqttPublisher`. One
source of truth, one replay path.
**Severity:** HIGH

### A6. `test_integration.py` hand-rolls `Config` — CLAUDE.md fixture rule violation
**Concern:** test architecture
**Scope:** `tests/test_integration.py:19-39` (`_make_config`), `tests/test_config.py:5-14` (`_base_config`)
**Problem:** CLAUDE.md §Testing: *"Fixture-driven config. Use the
sample_config fixture in tests/conftest.py. Don't hand-roll Config objects per
test."* `test_integration.py:_make_config()` is a second Config factory with
a different node set (`golem`, `gordon` added) and different people MAC-count
structure. `test_config.py:_base_config()` is a third factory. Result: three
canonical shapes of "a valid config" drift across tests.
**Impact:** A schema change (e.g. adding a required field to `NodeConfig`)
requires editing three places, not one. Test readers have to figure out which
config shape is in play for each test. The `sample_config` fixture's value is
eroded.
**Recommendation:** `sample_config` in `conftest.py` should expose enough
nodes/people for integration scenarios, OR the integration file should
parametrize `sample_config` via a fixture factory (e.g. `config_with_nodes`).
`test_config.py` necessarily hand-rolls to test validation boundaries — that's
fine — but integration should reuse.
**Severity:** HIGH

### A7. Config defaults duplicated between field default and `from_dict`
**Concern:** maintainability
**Scope:** `src/openwrt_presence/config.py:41-44,127-136`
**Problem:** `away_timeout: int = 64800`, `poll_interval: int = 30`,
`exporter_port: int = 9100`, `dns_cache_ttl: int = 300` all appear **twice**:
once as the dataclass field default, once as a `data.get("key", <default>)`
fallback in `from_dict`. The dataclass default is effectively dead code
because `from_dict` is the only documented constructor path and always
supplies a value. `departure_timeout` has no default and is required — which
is correct but inconsistent with the others.
**Impact:** Changing the 18h interior safety-net default requires editing
two lines or risks silent disagreement. There's no single "defaults" surface
for docs or review. Operator confusion — they can `Config(...)` directly
and get one default, or go via YAML and get another (identical today, but
easy to drift).
**Recommendation:** Centralize: either (a) pull defaults into module-level
constants (`DEFAULT_AWAY_TIMEOUT_SEC = 64800`) used in both places, or
(b) remove field defaults and require `from_dict`/`from_yaml` as the only
constructor, or (c) drop the `.get(...)` defaults and use field defaults
exclusively by passing only the keys present. Option (a) is least invasive.
**Severity:** HIGH

### A8. No runtime config reload — add-a-person requires restart
**Concern:** extensibility
**Scope:** `src/openwrt_presence/__main__.py:23-24`
**Problem:** Config is read once at startup via `Config.from_yaml`. Every
downstream object takes it by reference and stores derived state
(`ExporterSource._tracked_macs`, `PresenceEngine._last_person_state` keyed on
people, `MqttPublisher._topic_prefix`). No SIGHUP handler, no file-watcher, no
reload mechanism.
**Impact:** Adding a new device MAC, adding a new person, or adjusting
`away_timeout` because the bedroom phone keeps dozing requires a container
restart → LWT fires → HA marks entities unavailable → alarm automations see a
transient. That's the *opposite* of what the system is trying to provide.
**Recommendation:** This is a deliberate trade-off worth documenting either
way. If restart-only is the answer, CLAUDE.md should say so and the examples
should note it. If reload is desired: wire `SIGHUP` to reparse `Config`,
diff against old, and mutate `engine._devices` / `publisher._topic_prefix` /
`source._tracked_macs` with care. The engine's `_last_person_state` needs to
gain new people (AWAY default) and lose deleted ones. Feasible but not free.
Marking as HIGH because operationally surprising, not security-critical.
**Severity:** HIGH

---

## MEDIUM

### A9. Hardcoded `_QOS = 1`, retention, topic structure — no operator knobs
**Concern:** extensibility / maintainability
**Scope:** `src/openwrt_presence/mqtt.py:13,33-38,58-83`
**Problem:** `_QOS = 1` module constant, `retain=True` literal on every
publish, topic layout `homeassistant/device_tracker/{person}_wifi/config` hardcoded. No MQTT TLS support in `__main__._run` (no `tls_set()` call). HA
discovery `identifiers` and `name` hardcoded to `"openwrt_presence"` /
`"OpenWrt Presence"`.
**Impact:** Using this service on a shared broker with non-HA consumers needs
code edits. Running two instances of the service (e.g. primary + backup)
collides on `identifiers` — same HA device block. No way to turn off HA
discovery for ops who configure HA by YAML. No TLS means MQTT broker on the
public internet is unsafe.
**Recommendation:** Add `mqtt.discovery_prefix` (default `homeassistant`),
`mqtt.device_id` (default `openwrt_presence`), `mqtt.tls` config section
(optional, wire `client.tls_set()` in `_run`). Keep `QoS=1` and `retain=True`
— per CLAUDE.md they're contracts, not preferences — but make that an
explicit `_QOS = 1  # contract: see CLAUDE.md` and cover with a test that
*actually asserts* qos/retain on every published frame (see A2).
**Severity:** MEDIUM

### A10. Source layering inversion — `exporters.py` imports from `engine.py`
**Concern:** maintainability
**Scope:** `src/openwrt_presence/sources/exporters.py:11`
**Problem:** `from openwrt_presence.engine import StationReading`. The engine
module is the "upper" layer (consumes data); sources are "lower" (produce
data). Having sources reach up into the engine for the result type creates a
cycle risk and makes it impossible to use the source library without pulling
`datetime`-and-state-machine-adjacent code along.
**Impact:** A future second source (e.g. `sources/mqtt_ingest.py`) must
import from `engine` too — growing the import graph. If `engine.py` grows a
heavy dependency (say, `numpy` for RSSI smoothing), all sources inherit it.
**Recommendation:** Move `StationReading` (and arguably `StateChange`,
`PersonState`) to `openwrt_presence/domain.py`. `engine.py`, `mqtt.py`,
`sources/*.py`, `logging.py` all import from `domain`. Establishes the
dataless-vocabulary layer the architecture is already implicitly using.
**Severity:** MEDIUM

### A11. `_compute_person_state` hardcodes state-machine semantics inline
**Concern:** extensibility (state machine)
**Scope:** `src/openwrt_presence/engine.py:231,249,235`
**Problem:** "Home" is defined by `tracker.state in (DeviceState.CONNECTED,
DeviceState.DEPARTING)` in line 231. Room selection requires
`DeviceState.CONNECTED` in line 235. DEPARTING-room fallback in line 249.
Adding a fourth state (e.g. `DOZING` — phone seen 10 min ago but not now; or
`ROAMING` — deliberately transitional) means auditing every `DeviceState`
check across the engine.
**Impact:** The state machine is otherwise clean and well-isolated, but these
tuple-literal checks are the kind that grow stale silently. A new state
added without updating `_compute_person_state` produces invisible "not home"
bugs.
**Recommendation:** Two options: (a) a `DeviceState.is_present()` property
returning `self in {CONNECTED, DEPARTING}`, and a
`DeviceState.contributes_to_room()` returning `self is CONNECTED` — putting
the semantic knowledge on the enum; or (b) explicit sets at module level
(`PRESENT_STATES = frozenset({CONNECTED, DEPARTING})`). Option (a) scales
better.
**Severity:** MEDIUM

### A12. No end-to-end test of the dead-AP → departure safety claim
**Concern:** test architecture
**Scope:** (missing) tests exercising `ExporterSource` failure + engine + publisher together
**Problem:** CLAUDE.md §Security-critical says "a dead AP can generate false
departures". The mitigations (exit-vs-interior timeout split, 18h safety net)
are claimed correct but tested only at the engine-unit level
(`test_engine.py:TestExitNodeTimeouts`) with synthetic empty snapshots. No
test feeds a real `ExporterSource.query()` failure (or a composite where one
AP fails) through the engine to verify that (a) the failure doesn't crash the
loop, (b) the resulting snapshot is treated as "these devices didn't appear
from this AP" rather than "these devices are globally absent", (c) after
`away_timeout` the departure fires.
**Impact:** The alarm pathway's worst case — an AP dying during an extended
power outage at 3 AM — is not tested end-to-end. Unit tests prove the
engine is correct *given* empty snapshots; they don't prove
`ExporterSource` produces the right shape of empty snapshot on partial
failure.
**Recommendation:** Add an integration test with a `FakeSource` that
simulates one AP returning readings and another raising. Drive `_run()` (see
A3) for several "minutes" of injected time and assert the `StateChange`
sequence includes the correct departure(s) only after `away_timeout`.
**Severity:** MEDIUM

### A13. No metrics/health endpoint — observability will bolt on awkwardly
**Concern:** extensibility
**Scope:** `src/openwrt_presence/__main__.py` (no HTTP surface)
**Problem:** Service runs as a pure consumer→producer pipeline, no HTTP
port. No `/health` for Docker `HEALTHCHECK`, no `/metrics` for
self-monitoring (poll latency, AP health counts, state-change rate). The
only liveness signal today is "LWT fired" → HA goes unavailable, which is
*outcome-level* detection, not *operational* detection.
**Impact:** Adding Prometheus-style self-metrics later means either
importing a full HTTP server (aiohttp is already a dep, so not new weight)
or running a sidecar. Docker healthcheck currently has no good signal.
**Recommendation:** Optional `health.port` config key; when set, spin a
minimal `aiohttp.web` app in `_run` serving `/health` (200 if the poll loop
has run within `3 * poll_interval`) and `/metrics` (counters for
`polls_total`, `poll_errors_total`, `node_unreachable_total`,
`state_changes_total{person=...}`, histogram of `poll_duration_seconds`).
Engine and source expose counters; publisher reports per-person state gauge.
**Severity:** MEDIUM

---

## LOW

### A14. Magic sentinel RSSI values
**Concern:** maintainability
**Scope:** `src/openwrt_presence/engine.py:49,198,224`
**Problem:** `_DeviceTracker.rssi: int = -100` (line 49) as "initial / unknown"
value. `_best_representative` uses `best_rssi_val = -200` (line 198).
`_compute_person_state` uses `best_rssi: int = -200` (line 224). Three
unexplained numeric sentinels for "impossibly low RSSI". `-100` is also a
*plausible* real RSSI, so using it as an initial value is mildly lossy (a
device that has never been seen compares equal to a device at the edge of
range).
**Impact:** Reader has to infer what `-100` means. A device with genuine
`-100dBm` reading (edge of range, unusual but possible) is distinguishable
from "never seen" only by type (`rssi: int` vs `rssi: int | None`). Since
`StationReading.rssi` is always `int` not `int | None`, the engine cannot
represent "unknown but tracker exists".
**Recommendation:** Module-level `_IMPOSSIBLE_RSSI_FLOOR = -200` named
constant. Consider making `_DeviceTracker.rssi: int | None = None` with the
comparison sites handling `None`. Minor, but removes two unexplained numbers.
**Severity:** LOW

### A15. `test_mqtt.py` imports `call` from unittest.mock but never uses it
**Concern:** test architecture
**Scope:** `tests/test_mqtt.py:4`
**Problem:** Unused import `call`. Trivial but signals the tests were
originally written with call-matching in mind, then simplified to string
contains — which made things worse (A2).
**Impact:** Noise.
**Recommendation:** Remove when doing A2.
**Severity:** LOW

### A16. `monitor.py` CLI is untested
**Concern:** test architecture
**Scope:** `src/openwrt_presence/monitor.py`, (no corresponding test file)
**Problem:** The `openwrt-monitor` entry-point (pyproject.toml:17) parses
JSON log lines from stdin and pretty-prints. Zero tests. It's a dev-ergonomics
tool, not alarm-critical, so the severity is low — but it does parse the
exact JSON shape emitted by `log_state_change`, so a drift between them is
silently user-facing (operator sees `?` for a newly-added field).
**Impact:** Minor; operator confusion on log schema changes.
**Recommendation:** One table-driven test: feed a `StateChange` through
`log_state_change` with a capturing stream, read the JSON back, feed it to
`monitor._format_state_change`, assert the rendered string contains the
relevant fields. Catches schema drift with ~20 LOC.
**Severity:** LOW

### A17. `engine.tick` is documented but never called from production code
**Concern:** maintainability
**Scope:** `src/openwrt_presence/engine.py:137-154`, `src/openwrt_presence/__main__.py`
**Problem:** `PresenceEngine.tick(now)` exists, is tested
(`test_engine.py:TestTick`, `TestExitNodeTimeouts.test_tick_respects_node_timeout`),
and is public API. But `__main__._run` never calls it — it relies on
`process_snapshot([])` inside the poll loop to expire DEPARTING deadlines as a
side-effect. `tick` is dead code in production, alive in tests.
**Impact:** Two code paths for "expire departure timers" — `tick` and the
in-line expiry inside `process_snapshot` (lines 110-117). Both are tested,
but only the latter runs in production. A bug fix to one is easy to forget
in the other. CLAUDE.md §Architecture rules says "One code path per feature" —
this violates it.
**Recommendation:** Either (a) delete `tick` (and its tests), noting
`process_snapshot([])` is the expiry path; or (b) have `__main__._run` call
`engine.tick(now)` between `query()` returning and `process_snapshot` running,
and remove the in-line expiry loop from `process_snapshot`. (b) is cleaner —
it separates "process new readings" from "advance time" — but is more churn.
**Severity:** LOW

### A18. Dataclass for `StationReading`/`StateChange`/`PersonState` are *not* frozen
**Concern:** maintainability
**Scope:** `src/openwrt_presence/engine.py:19,28,39`
**Problem:** CLAUDE.md §Type system: "`@dataclass(frozen=True)` for config /
value objects, plain `@dataclass` for mutable trackers." `_DeviceTracker` is
correctly mutable. But `StationReading`, `StateChange`, `PersonState` are
value objects by nature (they cross module boundaries as data, never get
mutated in place) yet are declared with plain `@dataclass`. Inconsistent with
the rule.
**Impact:** A future maintainer reassigning fields on a `StateChange` in a
callback would silently mutate a value that another subscriber still holds a
reference to. Not a current bug.
**Recommendation:** `@dataclass(frozen=True)` on the three value classes.
Requires only checking no code mutates them — quick grep. `StationReading`
is constructed once in `_parse_metrics` and never modified; `StateChange` is
built in `_emit_changes`/`get_person_snapshot`, emitted, and read-only
downstream; `PersonState` is returned from `_compute_person_state` and
compared with `==`.
**Severity:** LOW

### A19. `departure_timeout` required but `away_timeout` defaulted — asymmetric
**Concern:** maintainability / operator UX
**Scope:** `src/openwrt_presence/config.py:124,127`
**Problem:** `departure_timeout` has no `.get()` fallback — missing = crash.
`away_timeout` defaults silently to 64800. An operator who forgets the latter
gets an 18h safety net silently; an operator who forgets the former gets a
`KeyError` — and not a `ConfigError` with a helpful message, because line 124
uses subscript not `.get`.
**Impact:** `KeyError: 'departure_timeout'` is a bad operator experience
compared to `ConfigError("departure_timeout is required; typical value: 120 (seconds)")`.
**Recommendation:** Raise `ConfigError` explicitly with guidance when
required fields are missing. Line 86 has the same `mqtt_raw["host"]`
subscript-shape; wrap those too. This isn't about hiding errors — per
CLAUDE.md errors must be loud — but about the quality of the error message.
**Severity:** LOW

---

## What's genuinely well-extensible

Call out fair points to avoid an all-negative read:

- **`PresenceEngine` boundary** is clean. `process_snapshot(now, readings) → list[StateChange]` is a single, testable surface with injected time. Adding a new state type requires editing only `engine.py` (plus A11 if new states affect "home" definition).
- **`Config.timeout_for_node`** correctly owns the exit/interior split — no duplication. Every caller already uses it.
- **`log_state_change` as a public function** means the audit line is a
  module-level concern, not bolted onto `MqttPublisher`. If a non-MQTT publisher
  is added later, it can call `log_state_change` the same way.
- **Tests for the engine (`test_engine.py`, `test_integration.py`)** genuinely
  follow the CLAUDE.md rules: real engine, injected `_ts(minutes)`, assertions
  on `StateChange` lists and `PersonState`. `TestExitNodeTimeouts` and
  `TestArrivalAndRoaming` are exactly the right shape. These are the reference
  for how A2 should be rewritten.
- **Node health tracking** (`ExporterSource._node_healthy`) is a small, well-
  scoped bit of cleverness with explicit transition-only logging — the right
  call for operational sanity.
