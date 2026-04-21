# Architecture Review — Type-System Leverage & Invariant Correctness

**Scope:** `/srv/eve` (~975 LOC src), Python 3.11+, frozen dataclasses + enums house-style.
**Lens:** what invariants the type system already enforces, and what safety-critical invariants still ride on convention, comments, or runtime defensiveness.

## Summary

The codebase is, for its size, noticeably disciplined: `DeviceState` is a real
enum; value objects are frozen dataclasses; `dict[str, Any]` is confined to
`Config.from_dict` as the CLAUDE.md contract promises; `TYPE_CHECKING` blocks
are used correctly to avoid runtime cycles; `datetime` is injected rather than
read from a clock. That said, several identifier spaces that the domain treats
as distinct (MAC address, person name, node name, room name, topic prefix)
are all typed as bare `str`, so the compiler cannot catch the canonical
mix-up (`person` passed where `mac` is expected, etc.). The biggest structural
hole is `StateChange`: its optional fields (`room`, `rssi`) encode a state
machine (CONNECTED vs AWAY) but the type permits every combination, including
the nonsensical ones that the engine actually produces at startup for
never-seen people — `home=False` with `mac=""`, `node=""`, which downstream
MQTT code then publishes as empty retained strings. A discriminated union
(`HomeState` / `AwayState`) would make those illegal states unrepresentable.
Secondary issues: `Config` is mutable though it behaves as a value object;
`_mac_lookup` is a semi-private field leaking into the public dataclass
signature; MAC normalisation is enforced by two separate call-sites rather
than by a `Mac` newtype; `paho` callbacks and the MQTT client are typed as
`Any`, dissolving the tidy boundary everywhere else.

Overall verdict: the type system is used **well above average** for a small
Python project, but a handful of targeted changes would move several
security-relevant invariants from "convention" to "compiler-enforced".

---

## CRITICAL

### A1. `StateChange` allows illegal combinations; startup actually produces them

**Concern:** type system leverage — discriminated union missing on the domain event that drives the alarm.
**Scope:** `src/openwrt_presence/engine.py:27-36`, `179`, `270-281`; `src/openwrt_presence/mqtt.py:97-123`; `src/openwrt_presence/__main__.py:85-87`.
**Problem:** `StateChange` is a single dataclass with fields

```python
person: str
home: bool
room: str | None   # None when away
mac: str
node: str
timestamp: datetime
rssi: int | None = None
```

The comment "None when away" is the entire invariant. The type system
permits `home=True, room=None` (impossible per `_compute_person_state` — a
home person always has a room) and `home=False, room="kitchen"` (impossible
too). More importantly, `_best_representative` and `get_person_snapshot`
explicitly produce a genuinely nonsensical value for never-seen people:
`mac=""`, `node=""`, `rssi=None` (engine.py:192-193). `__main__._run` then
calls `publisher.publish_state(snapshot)` on every person at startup
(`__main__.py:85-87`), and `_emit_state` publishes those empty strings as
**retained** MQTT payloads (`mqtt.py:113-123`) on a security-critical topic.
The sentinel `""` for "we have no idea" is not distinguishable from a real
value at the type level.

**Impact:**
- HA receives `"mac": "", "node": ""` on startup for never-seen people — a
  retained, valid-looking JSON attribute payload with garbage fields. An
  automation keying off `mac` would silently treat `""` as a device.
- Any future consumer that assumes `room is not None → home is True` (or
  the converse) is guessing, not reading a type. A refactor that flips a
  branch wrong is not a compile error.
- `home=False, room=<last known>` is a future footgun: it would be a
  plausible-looking hybrid the engine doesn't currently emit, but nothing
  stops a patch from doing so.

**Recommendation:** make `StateChange` a discriminated union. Something like:

```python
@dataclass(frozen=True)
class HomeState:
    person: PersonName
    room: str                    # always present
    mac: Mac                     # always a real MAC
    node: NodeName
    timestamp: datetime
    rssi: int                    # always present when we saw them
    home: Literal[True] = True

@dataclass(frozen=True)
class AwayState:
    person: PersonName
    timestamp: datetime
    # last-known context, explicitly optional:
    last_mac: Mac | None = None
    last_node: NodeName | None = None
    home: Literal[False] = False

StateChange = HomeState | AwayState
```

Then `_best_representative` either returns `None` or a populated triple —
the `("", "", None)` sentinel goes away. `_emit_state` pattern-matches
(`match change: case HomeState(): ...`) and can no longer publish `""`
as a MAC. The MQTT payload for `AwayState` drops the empty-string attributes
entirely.

**Severity:** CRITICAL

---

### A2. Never-seen-at-startup path publishes fabricated retained MQTT state

**Concern:** type system leverage — `Optional` missing on the engine API that is only sometimes meaningful.
**Scope:** `src/openwrt_presence/engine.py:160-179` (`get_person_snapshot`); `src/openwrt_presence/__main__.py:85-87`.
**Problem:** `get_person_snapshot` is documented as "always returns a value
regardless of whether the state has transitioned" and, for a never-seen
person, returns a `StateChange` with empty `mac`, empty `node`, `rssi=None`.
The caller (`__main__._run`) publishes *all* of them through the security-
critical `publish_state` path, which writes `retain=True` to three topics
including `attributes`. The type signature `StateChange` hides that this
is not a real change event — it's a "please seed HA with whatever I have,
even if I have nothing" request. The fact that MQTT then stores `"mac":
""` retained is a direct consequence of the type not distinguishing
"observation" from "no-observation".

**Impact:** on a clean install with an away person who's never been seen,
HA gets retained garbage attributes and an audit log line (`log_state_change`)
that says `mac="", node="", rssi=null`. That log line claims a state
transition happened; nothing transitioned.

**Recommendation:** make the absence explicit. Either:

- `get_person_snapshot(...) -> StateChange | None` and skip publish in
  `__main__` when `None` is returned; **or**
- split into `current_state(person) -> HomeState | AwayState` with the
  union above — `AwayState` with `last_mac=None` is a legitimate value
  and `_emit_state(AwayState(...))` simply doesn't include a mac in the
  payload.

**Severity:** CRITICAL (security-critical pathway publishes with
retention; audit log is misleading).

---

## HIGH

### A3. MAC, person, node, room are all `str` — zero type separation in the vocabulary

**Concern:** type system leverage — missing `NewType` / type aliases on the four identifier spaces the whole app is built on.
**Scope:** all modules. `config.py:38-39` (dict keys); `engine.py:19-42, 65, 74, 156, 185`; `sources/exporters.py:25, 56, 87-102`; `mqtt.py:27-55, 85`.
**Problem:** The app has four logically distinct identifier spaces:

- `Mac` — lowercase colon-separated hex
- `PersonName` — yaml key under `people:`
- `NodeName` — yaml key under `nodes:` (AP hostname)
- `Room` — free-form string in `NodeConfig.room`

All four are `str`. Consequences visible in the code:

- `PresenceEngine._compute_person_state(name: str)` and
  `get_person_snapshot(name: str, now)` accept any string; calling
  `get_person_snapshot("aa:bb:cc:dd:ee:01", ...)` compiles and returns
  a bogus "home=False, room=None" because `_mac_lookup` isn't consulted.
- `_best_representative` signature returns `tuple[str, str, int | None]`
  — reader has to trust the docstring to know that's `(Mac, NodeName, rssi)`.
- `ExporterSource(tracked_macs: set[str], ...)`: the constructor performs
  a defensive `{m.lower() for m in tracked_macs}` re-normalisation
  (`exporters.py:29`) precisely because the type doesn't carry the
  invariant "already normalised". `Config._normalize_mac` already ran
  at config load. This is a parse-don't-validate violation spelled out
  in the codebase: the receiver can't tell if the caller normalised, so
  it re-normalises.
- `StationReading.mac` is `str` with a comment `# lowercase, colon-separated`.
  `PresenceEngine.process_snapshot` still does `mac = r.mac.lower()` on
  line 86, again because the comment is not a type.
- `Config.mac_to_person(mac: str)` is forgiving (normalises on the way
  in), but that means "is this mac normalised?" can never be decided
  from a signature.

The mix-ups this could produce silently: passing a person name where a
MAC is expected (both are dict keys, both are `str`); writing
`f"{topic_prefix}/{mac}/state"` instead of `.../{person}/state` (all
strings).

**Impact:** one refactor away from a silent bug in the alarm pathway.
And three redundant normalisations that each cost nothing but each also
prove that the invariant isn't being carried.

**Recommendation:** four `NewType`s at the boundary:

```python
from typing import NewType
Mac = NewType("Mac", str)
PersonName = NewType("PersonName", str)
NodeName = NewType("NodeName", str)
Room = NewType("Room", str)
```

`Config._normalize_mac` returns `Mac`. `_mac_lookup: dict[Mac, PersonName]`.
`StationReading.mac: Mac`. `ExporterSource.__init__(tracked_macs: set[Mac])`
— and the `{m.lower() for m in tracked_macs}` defensive re-lowercasing goes
away (the caller already constructed `Mac`s). `PresenceEngine.process_snapshot(
now, readings: list[StationReading])` — the `r.mac.lower()` on engine.py:86
also goes away. Mix-ups between identifier kinds become compile errors
rather than silent misbehaviour.

This is the single highest-leverage type change available — the whole app
is about matching MACs to people, and both are currently `str`.

**Severity:** HIGH

---

### A4. `Config` is a mutable dataclass but semantically frozen

**Concern:** type system leverage — frozen-vs-mutable discipline (CLAUDE.md: "frozen for config / value objects, plain for mutable trackers").
**Scope:** `src/openwrt_presence/config.py:35-48`.
**Problem:** `NodeConfig`, `PersonConfig`, `MqttConfig` are correctly
`frozen=True`. But `Config` itself — the root of the config tree, the
singleton passed into every component — is `@dataclass` (mutable). Nothing
in the codebase ever mutates it. The CLAUDE.md rule says frozen for
"config / value objects", and `Config` is literally the config.

Also, `_mac_lookup: dict[str, str] = field(default_factory=dict, repr=False)`
is a *private implementation detail* (the leading underscore says so) that
nonetheless appears as a public dataclass field. It is:

- positional-settable in the `cls(...)` constructor call from `from_dict`
- part of the dataclass signature
- mutable at runtime (even if frozen, the `dict` inside isn't) — anyone
  can call `config._mac_lookup[new_mac] = "alice"` and alter routing.

**Impact:** a future caller could mutate `config.departure_timeout = 9999`
at runtime and nothing would complain; the engine would silently start
using the new value. Given the CLAUDE.md rule that this service drives
an alarm, that's a shared-mutable-state accident waiting to happen.
`_mac_lookup` mutation is worse — it re-routes MACs to different people.

**Recommendation:**
- `@dataclass(frozen=True)` on `Config`.
- Build `_mac_lookup` inside `from_dict` and store it in a frozen form —
  either as a `MappingProxyType` or as a tuple of `(Mac, PersonName)`
  pairs — and expose `mac_to_person` as the only access path. Don't have
  it as a constructor-visible field at all; make it a cached property
  computed from `people`, or keep it as `field(init=False, repr=False)`
  and set it via `object.__setattr__` (the conventional frozen-dataclass
  escape hatch) during `from_dict`.

**Severity:** HIGH

---

### A5. `paho` MQTT client and callbacks typed as `Any` — entire MQTT boundary untyped

**Concern:** type system leverage — "No `Any` in public signatures" (CLAUDE.md rule, emphasis original).
**Scope:** `src/openwrt_presence/mqtt.py:27, 45`; `src/openwrt_presence/__main__.py:37-50`.
**Problem:** `MqttPublisher.__init__(self, config: Config, client: Any)`.
That's a public signature. The CLAUDE.md file says in black and white
"No `Any` in public signatures." `_device_block` returns `dict[str, Any]`
— tolerable since it's a one-off discovery payload, but still cascades.

The `_on_connect` and `_on_disconnect` callbacks in `__main__.py:37-50`
have untyped `userdata`, `flags`, `disconnect_flags`, `reason_code`,
`properties` parameters (inference: `Any`).

**Impact:** the MQTT layer — the layer that talks to HA and drives the
alarm — is the least-typed part of the codebase. `client.publish(...)`
could be `client.publsih(...)` and mypy would not care. `paho-mqtt` 2.x
ships inline type hints and a `CallbackAPIVersion.VERSION2` specifically
so `on_connect`/`on_disconnect` signatures are known.

**Recommendation:**
- `client: paho.mqtt.client.Client` on `MqttPublisher.__init__`. `paho-mqtt`
  2.0+ has type hints. Import in a `TYPE_CHECKING` block if desired.
- Type the callbacks. Use `paho.mqtt.client.CallbackOnConnect` /
  `CallbackOnDisconnect` (or write an explicit `Callable[..., None]`).
  `reason_code` is `paho.mqtt.reasoncodes.ReasonCode`.
- `_device_block() -> HaDeviceBlock` with `HaDeviceBlock` as a
  `TypedDict` — since it's a wire format, `TypedDict` is the right
  tool (unlike the rest of the domain, which is dataclasses).

**Severity:** HIGH

---

## MEDIUM

### A6. `_DeviceTracker` state-invariant (`departure_deadline` iff `DEPARTING`) lives in logic, not types

**Concern:** type system leverage — state-machine invariant expressed via `Optional` instead of a tagged union.
**Scope:** `src/openwrt_presence/engine.py:44-52, 93-117`.
**Problem:** `_DeviceTracker` has:

```python
state: DeviceState = DeviceState.AWAY
node: str = ""
rssi: int = -100
departure_deadline: datetime | None = None
```

The actual invariant is:

- `CONNECTED` → `departure_deadline` MUST be `None`, `node`/`rssi` meaningful
- `DEPARTING` → `departure_deadline` MUST be set, `node`/`rssi` still meaningful (last-known)
- `AWAY` → `departure_deadline` MUST be `None`, `node=""`/`rssi=-100` are sentinels

The type permits all 2×4×N combinations. Correctness is enforced by the
engine following the three `if tracker.state == ...` blocks in order.
Line 113 actually defends (`tracker.departure_deadline is not None and
now >= ...`) — a defensive re-check that could be eliminated by the type
saying "a `DEPARTING` tracker has a deadline". The `node: str = ""`
sentinel has the same smell as A1: empty string standing in for "unknown".

**Impact:** a future patch that transitions AWAY → DEPARTING without
setting the deadline, or CONNECTED without clearing it, type-checks fine.
The `is not None` guard on line 113 papers over it at runtime. The
engine is a plain `@dataclass` (mutable tracker — the CLAUDE.md rule
says that's correct for mutable trackers), so the dial here is the
invariant structure, not frozen-vs-mutable.

**Recommendation:** either

- A tagged union of three frozen states: `ConnectedTracker(node, rssi)`,
  `DepartingTracker(node, rssi, deadline)`, `AwayTracker()`. The engine
  rebinds `self._devices[mac] = DepartingTracker(...)` rather than
  mutating fields. `_compute_person_state` pattern-matches. Stronger but
  a bigger refactor.
- Or, pragmatically: keep the mutable tracker, but add an assertion at
  the end of `process_snapshot` (or — better — a `_check_invariant` that
  runs in tests only) so the "DEPARTING ⇔ deadline set" contract is at
  least tested. The defensive `is not None` on line 113 can then be
  dropped or converted to an `assert`.

**Severity:** MEDIUM

---

### A7. `StationReading` mutable though semantically a read-only value

**Concern:** type system leverage — frozen discipline.
**Scope:** `src/openwrt_presence/engine.py:18-25`.
**Problem:** `StationReading` is `@dataclass` (not frozen). It's a pure
value: a tuple `(mac, ap, rssi)` sampled at a single instant. Per
CLAUDE.md "frozen for config / value objects, plain for mutable trackers",
and per the constructor-only usage (`_parse_metrics` builds them,
`process_snapshot` reads), it should be frozen. Making it frozen means
`process_snapshot` can't do `r.mac = r.mac.lower()` — but it already
constructs a new one explicitly (engine.py:90) and never mutates an
existing reading, so nothing breaks.

**Impact:** the difference between "I observed this reading" and
"something later mutated it" is untyped. Low blast radius but a free
consistency win.

**Recommendation:** `@dataclass(frozen=True)` on `StationReading` and
`PersonState`. (`PersonState` is compared with `==` in `_emit_changes`
as an equality check; frozen still supports that.)

**Severity:** MEDIUM

---

### A8. `Config.from_dict` uses `data.get("mqtt", {})` then indexes required fields

**Concern:** type system leverage — parse-at-boundary half-measure; `.get()` fallback on a required section contradicts the "No `.get()` fallbacks on required config" rule.
**Scope:** `src/openwrt_presence/config.py:84-91`.
**Problem:**

```python
mqtt_raw = data.get("mqtt", {})
mqtt = MqttConfig(
    host=mqtt_raw["host"],
    port=mqtt_raw["port"],
    topic_prefix=mqtt_raw["topic_prefix"],
    ...
)
```

If `mqtt:` is missing from YAML, `data.get("mqtt", {})` yields `{}`, then
`mqtt_raw["host"]` raises `KeyError: 'host'` — not a `ConfigError`. For
`nodes:` and `people:` the code does the right thing and raises a typed
`ConfigError`. CLAUDE.md: "Missing required fields must raise `ConfigError`".

**Impact:** startup failure with a `KeyError` instead of a helpful
"mqtt section missing" message. Operational papercut, not a safety bug.

**Recommendation:** mirror the `nodes` / `people` style — `if "mqtt" not
in data: raise ConfigError("mqtt section required")` — or wrap the whole
`from_dict` body in a try/except that re-raises `KeyError` as
`ConfigError(f"missing required field: {e.args[0]}")`. Alternatively,
require that every section be present and hoist the `.get(...)` defaults
for optional scalar fields (`departure_timeout` is correctly required
via `data["departure_timeout"]`; follow the same pattern for `mqtt`).

**Severity:** MEDIUM

---

### A9. `_compute_person_state(name: str)` silently returns "away" for unknown persons

**Concern:** type system leverage — unknown lookup encoded as a normal return value.
**Scope:** `src/openwrt_presence/engine.py:212-220, 156-158, 185-193`.
**Problem:** `_compute_person_state`, `get_person_state`, `_best_representative`
all silently accept a bogus person name and return "away"/empty sentinels.
The engine holds `self._config.people.get(name)` — `None` → return
`PersonState(home=False, room=None)` (for `_compute_person_state`) or
`("", "", None)` (for `_best_representative`).

Per CLAUDE.md "Never default to 'home'" — this is not violated, the
defaults are "away" which is fail-secure. But the type signature is
indistinguishable from a legitimate "known person, actually away". The
only caller of `get_person_state(name)` is tests; in production the
only caller of `_compute_person_state` is the engine itself where `name`
comes from `self._config.people` iteration, so it's always known. So
this defensive branch is unreachable in production.

**Impact:** dead code path that creates the illusion of robustness;
bugs that pass an unknown person name silently succeed instead of
being caught.

**Recommendation:**
- `_compute_person_state(name: PersonName)` + assert `name in self._config.people`
  (or `raise KeyError`). It's a private method, the precondition is
  the caller's responsibility.
- `get_person_state(name: PersonName) -> PersonState` — same treatment.
- This couples nicely with A3 (`PersonName` newtype).

**Severity:** MEDIUM

---

## LOW

### A10. `NodeConfig.url` is `str | None` but the resolution always produces a `str`

**Concern:** type system leverage — "computed default" pattern where a property could return a stronger type.
**Scope:** `src/openwrt_presence/config.py:14-19, 52-58`.
**Problem:** `NodeConfig.url: str | None`. The `Config.node_urls` property
resolves `None` to `f"http://{name}:{exporter_port}/metrics"`. So the
domain-level fact is "every node has a URL, some are explicit and some
are derived from the name". The `None` is an artefact of the YAML
representation. Downstream `ExporterSource(node_urls: dict[str, str])`
correctly takes the resolved form. OK.

**Impact:** none practically; but a reader of `NodeConfig` alone would
think "some nodes have no URL", which isn't true.

**Recommendation:** either rename the raw field to `url_override: str | None`
to make intent clear, or compute the resolved URL eagerly in `from_dict`
(needs the `exporter_port`, so the resolution has to happen in `Config`,
which it already does in `node_urls`). Low priority.

**Severity:** LOW

---

### A11. `monitor.py` uses `dict` (raw, unparameterised) — parses JSON into `Any`

**Concern:** type system leverage — CLI-only module consumes untyped JSON.
**Scope:** `src/openwrt_presence/monitor.py:34, 57`.
**Problem:** `_format_state_change(data: dict)` / `_format_log(data: dict)`
take raw dicts (no type params). The content is produced by this very
codebase (`log_state_change` → `structlog` JSON), so the schema is known.

**Impact:** if the log line schema changes (renaming `event_ts` or
`presence`), `monitor` silently falls back to `?`. That's actually
`monitor`'s job (be resilient to unknown input), so a loose type is
defensible here. But there's no connection between the producer
(`log_state_change` in `logging.py`) and the consumer (`monitor.py`)
at the type level.

**Impact:** low — `monitor` is a convenience CLI, not on the security path.
**Recommendation:** optional. Share a `TypedDict` / dataclass between
producer and consumer, or leave as-is with an explicit comment that
monitor is schema-tolerant on purpose.
**Severity:** LOW

---

### A12. `PersonConfig.macs` is a mutable `list[str]` inside a frozen dataclass

**Concern:** type system leverage — frozen dataclass with mutable-collection field.
**Scope:** `src/openwrt_presence/config.py:21-23`.
**Problem:** `@dataclass(frozen=True) class PersonConfig: macs: list[str]`.
The dataclass is frozen but the list isn't — `config.people["alice"].macs.append("ff:ee:dd:cc:bb:aa")`
works. Real frozen semantics would use `tuple[Mac, ...]` or `frozenset[Mac]`.

**Impact:** low, since nothing mutates it. But it weakens the "frozen =
safe to share" guarantee that the rest of the codebase reads as a contract.

**Recommendation:** `macs: tuple[Mac, ...]` (or `frozenset[Mac]` if order
never matters — it doesn't appear to). Combine with A3.
**Severity:** LOW

---

### A13. `StateChange.home: bool` is redundant with `room is not None`

**Concern:** type system leverage — two fields encoding one datum.
**Scope:** `src/openwrt_presence/engine.py:27-35`, `mqtt.py:98`.
**Problem:** `home` and `room` co-vary: `home is True` iff `room is not None`
(per `_compute_person_state`, which either sets both or neither). Two
fields mean two ways to disagree. `mqtt._emit_state` branches on `change.home`
but reads `change.room` — a future caller constructing a `StateChange`
with `home=True, room=None` compiles fine and produces a home state with
empty room string in MQTT. Subsumed by A1's discriminated-union fix
(the `home` field becomes a tag, always correct by construction).
**Severity:** LOW (redundant with A1's fix)

---

## What's genuinely well-done

Not all bad news. The codebase already does several things right that
are rarer than they should be in small Python services:

- `DeviceState` is an `Enum`, not a set of string constants. The
  CLAUDE.md rule "Enums over string constants for state" is honoured.
- `datetime` is passed in, never read from a clock inside the engine.
  The tests exploit this cleanly (`_ts(minutes)`). No
  `datetime.now()` monkey-patching.
- `dict[str, Any]` is, as promised, confined to the YAML parse boundary
  (`Config.from_dict` and `MqttPublisher._device_block`). The rest of
  the codebase speaks domain types.
- `TYPE_CHECKING` blocks used correctly in `engine.py`, `mqtt.py`,
  `logging.py` to avoid import cycles without polluting runtime.
- `Config.timeout_for_node` is the single owner of the exit-vs-interior
  logic, as the architecture note demands. No duplication found.
- `MAC` normalisation is *centralised in two places* (which is one too
  many — see A3 — but still: `_normalize_mac` and `_parse_metrics` both
  do it and downstream code doesn't re-do it ad hoc).
- Fail-secure defaults: unknown MAC → no effect; unknown person → away;
  empty snapshot → eventual DEPARTING. The "never default to home" rule
  is upheld.
- Frozen-vs-mutable is mostly correct: `NodeConfig`, `PersonConfig`,
  `MqttConfig` frozen (value); `_DeviceTracker` mutable (tracker — per
  the CLAUDE.md rule). Only `Config` itself (A4) and `StationReading` /
  `PersonState` (A7) break the pattern.

---

## Suggested order of attack

If only three fixes happen, in priority order:

1. **A1 + A2** (the `StateChange` discriminated union) — closes the
   retained-garbage-MQTT issue on the security-critical path.
2. **A3** (`Mac` / `PersonName` / `NodeName` `NewType`s) — highest
   leverage per line changed, eliminates two redundant normalisations,
   makes A9's precondition expressible.
3. **A4** (freeze `Config`, hide `_mac_lookup`) — converts the root
   config object from "shared mutable dict of truth" to an actual value.

A5 (typing the MQTT boundary) is independently valuable and can land any
time. The rest are polish.
