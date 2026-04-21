# Architecture Review — Agent A1: Abstraction Boundaries

Reviewed the ~975 LOC `src/openwrt_presence/` layout end-to-end through
the lens of boundaries, responsibility & cohesion, and dependency
direction. Order of read: `CLAUDE.md`, `__init__.py` (empty),
`__main__.py`, `config.py`, `engine.py`, `mqtt.py`,
`sources/exporters.py`, `logging.py`, `monitor.py`. Also checked the
test layout and the `sources/` subpackage shape for intent.

Overall verdict: boundaries are **mostly clean**. `engine.py` is
genuinely pure, `config.py` parses at the boundary, the poll loop is
cleanly injected. Six things are structurally noteworthy — none are
CRITICAL (no god objects, no circular imports, no I/O in the engine)
but two are HIGH because they encode implicit caller-ordering
obligations in a security-critical codebase. The rest are MEDIUM/LOW
cleanups whose main cost is future drift, not current incorrectness.

---

## HIGH

### A1. `MqttPublisher.__init__` silently mutates the injected client (LWT must-fire-first ordering)
**Concern:** responsibility & cohesion / interface contracts
**Scope:** `src/openwrt_presence/mqtt.py:27-38`, `src/openwrt_presence/__main__.py:28-52`
**Problem:** the constructor calls `self._client.will_set(...)` as a
side effect on the injected paho client. This creates a hidden
must-call-sequence invariant: `MqttPublisher` **must** be
instantiated *before* `client.connect_async()`. The current
`__main__._run` happens to satisfy this (line 34 before line 52), but
the contract is not expressed in types, not documented on the
constructor, and not checkable at runtime. A well-intentioned refactor
that moves publisher construction after connect would silently
eliminate the LWT — which `CLAUDE.md` flags as a security-critical
invariant ("LWT must fire. ... Don't remove it").
**Impact:** the alarm-facing safety net depends on an ordering rule
that exists only by convention. Any future change (lazy publisher
construction, tests that construct the publisher for a connected
client, adding a `PublisherFactory`) can break LWT without any test
failing — the MQTT client would simply not set the will, and HA would
never mark entities unavailable on a crash.
**Recommendation:** make the LWT setup explicit and ordering-obvious.
Either (a) add a classmethod `MqttPublisher.attach(client, config)`
that asserts `client.is_connected() is False` and sets the will with a
docstring stating "must be called before connect", or (b) pull
`will_set` out of the constructor into `__main__._run` next to the
other pre-connect client setup so the sequence is visible in one
place. The publisher should take `client` as a pure collaborator and
not mutate it at construction time.
**Severity:** HIGH

### A2. `MqttPublisher.publish_state` owns audit logging — transport couples to audit trail
**Concern:** responsibility & cohesion / dependency direction
**Scope:** `src/openwrt_presence/mqtt.py:85-95`, `src/openwrt_presence/logging.py:46-57`
**Problem:** `publish_state` does three things — cache the change,
publish to MQTT, and call `log_state_change`. The in-code comment
justifies this as "the single code path so callers can never forget
one half". That's a valid invariant, but it is encoded in the wrong
place: the MQTT transport now *owns* the audit trail. Also,
`logging.py` depends on `engine.StateChange` (via `TYPE_CHECKING`) —
so a general-purpose logging module has a domain-model import. The
audit-log function is actually a domain concern that was filed under
"logging" because it emits a log line.
**Impact:** (a) if a second sink is ever added (Prometheus counter,
webhook, second broker), the audit log is either double-emitted, moved
again, or silently lost depending on which sink "wins". (b) In tests,
any test that mocks the MQTT publisher also loses the audit line
without saying so. (c) The dependency direction is mildly inverted: a
util module (`logging.py`) now knows about a domain type
(`StateChange`).
**Recommendation:** introduce a thin facade `PresenceSink` (or fold it
into `__main__._run`) that is the single callsite for "a state change
happened": it emits the audit log and delegates to all publishers.
Move `log_state_change` out of `logging.py` into `engine.py` (next to
`StateChange`) or a new `audit.py` — the function is a domain
operation, not a logging utility. `logging.py` should retain only
`setup_logging()`.
**Severity:** HIGH

---

## MEDIUM

### A3. `StationReading` defined in `engine.py`, produced by `sources/exporters.py` — producer imports from consumer
**Concern:** dependency direction / abstraction boundaries
**Scope:** `src/openwrt_presence/engine.py:18-24`, `src/openwrt_presence/sources/exporters.py:11`
**Problem:** `StationReading` is the source→engine contract. It is the
*output* of the source and the *input* of the engine. It currently
lives in `engine.py`, which forces `sources/exporters.py` to import
from `engine`. The source module has a hard dependency on the engine
module for the type of thing it produces. Structurally this is
backwards; the engine should depend on the source's contract, not the
source on the engine's internals.
**Impact:** (a) any new source (e.g. `sources/hostapd.py`, `sources/ubus.py`
— the plural `sources/` subpackage signals future intent) will also
`from openwrt_presence.engine import StationReading`, further
entrenching engine as the home of a non-engine type. (b) if you ever
want to test the source in isolation without the engine importable,
you can't. (c) The engine module's public surface now includes an
input DTO alongside its output DTOs (`StateChange`, `PersonState`),
which blurs its role.
**Recommendation:** move `StationReading` to either
`src/openwrt_presence/sources/__init__.py` (contract lives with the
producer) or a neutral `src/openwrt_presence/types.py`. The engine
imports it; the sources produce it. Define a lightweight `Protocol`
`Source` with `async def query() -> list[StationReading]` and `async
def close() -> None` while you're there — it's what the plural
`sources/` namespace is already hinting at.
**Severity:** MEDIUM

### A4. `__main__` computes `tracked_macs` — Config-knowledge leaks into the composition root
**Concern:** abstraction boundaries / cohesion
**Scope:** `src/openwrt_presence/__main__.py:55-63`, `src/openwrt_presence/config.py:110-148`
**Problem:**
```python
tracked_macs={
    mac
    for person_cfg in config.people.values()
    for mac in person_cfg.macs
},
```
`__main__` is reaching inside `PersonConfig` to flatten MACs. `Config`
already computes this exact set as `_mac_lookup` (line 111-120) but
doesn't expose it. Every caller that needs "all tracked MACs" has to
rebuild it.
**Impact:** (a) MAC normalization lives in `Config._normalize_mac` but
the consumer in `ExporterSource` also lowercases again
(`exporters.py:29`) — because it can't trust `__main__`'s comprehension
to have normalized. (b) if MAC storage ever changes (e.g.
per-person enabled/disabled flag), `__main__` silently does the wrong
thing. (c) the composition root is doing business logic, not just
wiring.
**Recommendation:** add `Config.tracked_macs: frozenset[str]` as a
property that returns `self._mac_lookup.keys()` frozen. `__main__`
passes `config.tracked_macs` directly. One code path, owned by the
type whose invariant it is.
**Severity:** MEDIUM

### A5. `engine.tick()` is dead code with overlapping responsibility
**Concern:** cohesion
**Scope:** `src/openwrt_presence/engine.py:137-154`
**Problem:** `PresenceEngine.tick(now)` exists and iterates DEPARTING
devices to expire them to AWAY. But `process_snapshot` already does
exactly this expiry (lines 109-117). `tick` is never called from
`__main__._run` — the poll loop only calls `process_snapshot`. So
there are two methods that expire the same state, and only one is
wired up. The existence of `tick` signals an unimplemented design
(separate timer-driven expiry from snapshot-driven updates) that never
landed.
**Impact:** (a) confusion — a reader sees two expiry paths and has to
reconstruct which is authoritative. (b) if someone ever wires `tick`
into a separate timer without removing it from `process_snapshot`,
expiry runs twice with different `now` values in the same cycle — at
best idempotent, at worst emits two `StateChange`s for the same
transition. (c) tests may exercise `tick` and give false confidence
that the in-production path is tested.
**Recommendation:** pick one. Either delete `tick` (the poll loop is
the only consumer and `process_snapshot` already handles expiry), or
extract the DEPARTING→AWAY expiry block out of `process_snapshot`,
have `process_snapshot` call `tick(now)` first, and document that
`tick` can also be called standalone between snapshots. Given the
current poll-loop architecture, deletion is the simpler answer.
**Severity:** MEDIUM

### A6. `MqttPublisher._last_state` duplicates engine's last-per-person state
**Concern:** cohesion / single source of truth
**Scope:** `src/openwrt_presence/mqtt.py:31,93,148-149`, `src/openwrt_presence/engine.py:63,261-268`
**Problem:** both modules keep a "last per-person" cache.
`PresenceEngine._last_person_state` holds `PersonState` (used for
change detection). `MqttPublisher._last_state` holds `StateChange`
(used for reconnect replay). They're not identical — the publisher
needs the richer payload with mac/node/rssi/timestamp for replay —
but they represent the same conceptual thing. Meanwhile the engine
already exposes `get_person_snapshot(name, now)` which returns exactly
the `StateChange` the publisher needs.
**Impact:** (a) the publisher's replay at reconnect uses stale
mac/node/rssi (whatever was cached on the last transition), not
current-truth. On a long reconnect window where people moved rooms
but the cache's source transitions didn't fire, HA sees stale
attributes after reconnect rather than current state. (b) two caches
for one concept drift over time. (c) the `publish_state → cache`
coupling in `MqttPublisher` is another "invariant encoded by
convention" — a new caller that publishes without caching would break
replay.
**Recommendation:** delete `MqttPublisher._last_state`. On
`on_connected`, iterate `config.people` and call
`engine.get_person_snapshot(person, now)` for each — same pattern
`__main__._run` already uses at startup (lines 85-87). The engine is
then the single source of truth for person state; the publisher is a
pure sink. Note this requires `on_connected` to have access to
`engine` + current `now` — inject both, or expose a
`publisher.reseed_from(snapshots: list[StateChange])` method called
from `__main__` via a reconnect callback that queries the engine.
**Severity:** MEDIUM

---

## LOW

### A7. `monitor.py` and `log_state_change` share a JSON field contract with no type linkage
**Concern:** abstraction boundaries / interface contracts
**Scope:** `src/openwrt_presence/logging.py:46-57`, `src/openwrt_presence/monitor.py:34-54`
**Problem:** `log_state_change` emits keys `person`, `presence`,
`room`, `mac`, `node`, `rssi`, `event_ts`. `monitor._format_state_change`
reads those exact keys from stdin JSON. The contract between producer
and consumer is an implicit stringly-typed schema. Rename a field in
one place, the other silently falls back to `"?"` or `""`.
**Impact:** schema drift risk. Low in practice because both live in
one repo and the monitor is a dev tool, but the coupling is invisible
to the type system.
**Recommendation:** either extract a tiny `AuditLogEntry` TypedDict
that both sides consume, or accept the cost given monitor is a CLI
pretty-printer. If kept as-is, add a one-line comment on
`log_state_change` listing the consumer.
**Severity:** LOW

### A8. `sources/` subpackage has an implicit, undocumented Source protocol
**Concern:** interface contracts
**Scope:** `src/openwrt_presence/sources/`, `src/openwrt_presence/__main__.py:55-63,100-102`
**Problem:** the plural name `sources/` + single `ExporterSource`
inside it signals intent for multiple sources behind a shared
interface. No `Protocol`, `ABC`, or docstring defines the contract.
The consumer in `__main__` uses `source.query()` and `source.close()`
and relies on the return type by duck typing. A second source would
have to guess: are timeouts the source's job? Is DNS caching? Does
`close()` need to be idempotent? Does `query()` raise or swallow
per-AP failures?
**Impact:** future multi-source work (or test doubles) lacks a
checklist. Low severity because only one source exists today.
**Recommendation:** add a `Source` `Protocol` (or ABC) in
`sources/__init__.py` declaring `async def query() -> list[StationReading]`
and `async def close() -> None`, with a docstring that specifies the
failure contract (per-node errors isolated, not raised). Costs 10
lines, prevents drift the day a second source arrives.
**Severity:** LOW

### A9. `Config` is `@dataclass` (mutable) while its sub-configs are frozen
**Concern:** cohesion / type-system discipline
**Scope:** `src/openwrt_presence/config.py:35-45`
**Problem:** `NodeConfig`, `PersonConfig`, `MqttConfig` are all
`frozen=True`, but `Config` itself is not. The likely reason is
`_mac_lookup: dict[str, str] = field(default_factory=dict)` — a
frozen dataclass with a mutable default-factory field is fine, but
the dict itself is still mutable. In practice `_mac_lookup` is written
once in `from_dict` and never again, so freezing `Config` would work
(assignments go via the constructor, not via attribute mutation).
**Impact:** `CLAUDE.md` says "Frozen dataclasses for config / value
objects" — `Config` silently violates this. A future contributor could
add `config.poll_interval = 60` at runtime and no type checker would
complain. Given the alarm pathway, runtime mutation of config is a
risk worth closing off.
**Recommendation:** add `frozen=True` to `Config`. Keep the `dict`
inside; just forbid rebinding attributes. Trivial change, restores the
invariant CLAUDE.md claims.
**Severity:** LOW

### A10. `publisher.on_connected` name collides conceptually with paho's `on_connect`
**Concern:** interface contracts (naming, but with contract
implications)
**Scope:** `src/openwrt_presence/mqtt.py:138-149`, `src/openwrt_presence/__main__.py:37-50`
**Problem:** `publisher.on_connected()` is an application-level
reseed method; it is *called from* a paho `on_connect` callback in
`__main__`. The name strongly implies "this is the paho callback"
and readers may wire it directly, but its signature doesn't match
paho's (which takes `client, userdata, flags, reason_code, properties`).
Someone setting `client.on_connect = publisher.on_connected` would
get a runtime error.
**Impact:** small foot-gun. Doesn't cause bugs today because
`__main__` wraps it correctly, but the name does not communicate the
contract.
**Recommendation:** rename to `reseed_after_connect()` or
`republish_all()`. The docstring already says what it does — let the
name agree.
**Severity:** LOW

---

## What is clean and should stay that way

- `engine.py` is a real pure-logic module: no I/O, no `datetime.now()`,
  `now` injected at every entry point. The `_emit_changes` / last-state
  cache pattern is correct for change detection.
- `config.py` parses at the boundary, raises `ConfigError` with
  human-readable messages, exposes derived views (`node_urls`,
  `timeout_for_node`, `has_exit_nodes`) as properties/methods rather
  than forcing callers to recompute. The exit-vs-interior timeout logic
  has exactly one home.
- `ExporterSource` owns its own session/connector lifecycle, health
  tracking, and log-on-transition behavior. No leakage into the poll
  loop.
- `logging.py`'s `setup_logging()` is cleanly a side-effecting bootstrap
  with no domain coupling (the `log_state_change` bit is the problem
  — see A2).
- No circular imports, no global state, no module-level mutable
  singletons. The composition root in `__main__._run` does its job.
