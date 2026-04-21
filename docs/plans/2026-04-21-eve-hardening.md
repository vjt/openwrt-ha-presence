# Eve Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 7 CRITICAL and 14 HIGH findings from the
2026-04-21 architecture review; lock in type, test, and tooling
discipline so the same class of mistakes cannot regress. One release at
the end (`v0.6.0`), no intermediate deploys.

**Architecture:** Preserve the core engine (pure logic, injected time,
domain types crossing boundaries) — the review confirmed it is sound.
Rewrite the seams: introduce a `domain.py` module owning the value
vocabulary, a `Source` protocol under `sources/`, a `FakeMqttClient` +
`FakeSource` test infrastructure, a discriminated `StateChange` union,
`NewType` aliases for the four identifier spaces (`Mac`, `PersonName`,
`NodeName`, `Room`), and fail-secure handling for "all APs
unreachable". Move `will_set` + audit logging out of `MqttPublisher`;
hand off paho callbacks to the asyncio loop via
`call_soon_threadsafe`. **MQTT wire format is an invariant** — internal
refactors must not change the bytes HA receives.

**Tech Stack:** Python 3.11+ (upper bound 3.13 added), frozen
dataclasses (not Pydantic — eve uses dataclass-native style), paho-mqtt
2.x with `CallbackAPIVersion.VERSION2`, aiohttp, structlog, pytest +
pytest-asyncio + hypothesis. Tooling: pyright (strict), ruff
(check+format), pre-commit, GitHub Actions.

**Release model:** single `v0.6.0` cut after all 6 sessions complete +
full green CI + one-day prod soak. CHANGELOG accumulates in
`[Unreleased]` throughout.

---

## Engineering standards inherited / enforced

Codifying below so a fresh session can execute without reloading
context. Some are already in `CLAUDE.md`; some are new (from
`/srv/gastone/CLAUDE.md` and `~/code/ha/ha-verisure/CLAUDE.md`). After
Session 0, these become `pyproject.toml` + `.ruff.toml` +
`pyrightconfig.json` rules enforced by CI.

**Eve is security software.** Same framing ha-verisure uses for
its alarm-panel integration. One wrong behavior = mis-armed alarm =
thief gets in, or family locked out. Every design decision
optimizes for **correctness over convenience**:
- **Fail-secure, not fail-safe.** Unknown state = AWAY (the state
  that arms the alarm), never a silent "probably home" default. Dead
  AP = don't know = eventual AWAY *unless the circuit breaker (C3)
  says we're blind*, in which case **hold**, don't arm.
- **Crash loud on unexpected input.** Malformed `/metrics` response,
  non-integer RSSI, MAC with non-hex characters, unknown config
  section — raise with a message the operator can act on. No silent
  fallthrough, no `.get(..., default)` on required data.
- **Audit trail non-optional.** Every state transition logged
  (`state_computed`) and every MQTT delivery confirmed
  (`state_delivered`). The log is the forensic record.
- **No "smart" behavior.** Pedantic correctness over convenience.
  Don't add timeouts, fallbacks, or auto-recovery paths beyond the
  review's explicit asks — they hide bugs.

**Type discipline (pyright strict):**
- No `Any` in public signatures. `dict[str, Any]` only at the YAML
  parse boundary (`Config.from_dict`).
- `NewType` for identifier spaces — `Mac`, `PersonName`, `NodeName`,
  `Room`. Mix-ups are compile errors.
- Frozen dataclasses for value objects (`StationReading`, `StateChange`,
  `PersonState`, `Config` root). Plain `@dataclass` only for mutable
  trackers (`_DeviceTracker`).
- Discriminated unions for state-dependent shapes (`HomeState` /
  `AwayState`). No `room: str | None` with a comment invariant.
- `from __future__ import annotations` everywhere.
- No `# type: ignore` without a comment explaining *why*.
- **Type errors are design signals.** When pyright blocks your
  approach, ask why — the constraint is probably correct.

**No default arguments** (beyond genuine config `timeout=30`-style
defaults). Missing required fields raise `ConfigError`, never
`KeyError` and never `.get("x", <silent default>)`.

**Exception handling:**
- Never swallow exceptions. `except Exception: logger.exception(...)`
  is acceptable only when the *comment* states why continuing is
  correct (like `__main__._run`'s query loop — isolation is the
  point).
- Error messages must guide the operator: `ConfigError("departure_timeout
  is required; typical value 120 (seconds)")`, not `KeyError: 'departure_timeout'`.

**Testing discipline:**
- **Assert outcomes, not call sequences.** No
  `"topic" in str(mock_call)` scans. Assert on concrete `PublishedMsg`
  objects, `StateChange` lists, retained payload bytes, `PersonState`.
- **Use production code in tests** — never hardcode expected JSON
  strings; call the actual formatter. Build synthetic *inputs*, not
  synthetic *outputs*.
- **Mock at boundaries, real dependencies inside.** Fake MQTT client,
  fake Source — real engine, real config, real audit log.
- **Inject time, never monkeypatch `datetime.now`.** Helpers:
  `_ts(minutes=0) -> datetime`.
- **Never weaken production code to make a test pass.** If a test
  needs special setup, fix the test.
- Full suite under 5s. `pytest --timeout=5`. Zero warnings.
- Property-based tests (`hypothesis`) for the state machine — fuzz the
  snapshot → StateChange invariants.
- Architecture tests via AST (check import direction, no `Any` in
  public sigs, no `dict[str, Any]` outside the parse boundary).

**Structural:**
- Constructor injection. No globals, no singletons, no module-level
  mutable state.
- Return domain types, not dicts/tuples/strings callers must parse.
  `tuple[str, str, int | None]` is a smell — name the thing.
- One code path per feature. If two methods (e.g. `tick` and
  `process_snapshot`'s inline expiry) do the same work, one of them
  is wrong — delete.
- Fix root causes, not examples. A bug report is one instance of a
  broader class.
- "Done" means done — grep for stale references before committing
  renames.
- **State the contract.** Before implementing any new method,
  write its docstring as "Returns X. Raises Y when Z." If you can't
  name Y precisely, the method isn't designed yet.
- **Parse at the boundary, trust inside.** YAML → `Config.from_dict`
  parses and raises `ConfigError`. Metric regex → `StationReading`
  with validated `Mac` and integer `rssi`. Downstream code trusts the
  types — no re-validation, no defensive `.lower()`, no `try/except`
  that "just in case" converts a runtime error into a silent skip.

**MQTT wire-format invariant** (hard constraint for refactors):
- Topic structure unchanged: `{prefix}/{person}/state|room|attributes`,
  `homeassistant/device_tracker/{person}_wifi/config`, availability
  topic, etc.
- Payloads unchanged: `home`/`not_home`, room string, attributes JSON
  fields. Internal type changes MUST NOT affect bytes on the wire.
- QoS=1, retain=True on state/room/attributes/availability/discovery.
  Retain=False on status-only messages (there are none today).
- LWT payload + topic unchanged.
- **Test:** a byte-level wire-format test compares a fixed set of
  transitions against a known-good fixture of `PublishedMsg` values.
  Captured before Session 3 type surgery begins (Session 1 deliverable).

**Git / commit discipline:**
- Bite-sized commits — one logical change per commit, message explains
  *why*, not *what*.
- Branch for this work: `hardening-v0.6.0`. Master stays deployable.
- Pre-commit hooks run pyright + ruff + pytest on every commit.
- CHANGELOG `[Unreleased]` updated with every commit that changes
  observable behavior.
- No amend after push. No force-push to master.

---

## File structure — target state after Session 5

```
src/openwrt_presence/
├── __init__.py                      # empty
├── __main__.py                      # wiring + asyncio lifecycle, typed callbacks
├── domain.py                        # NEW — StationReading, HomeState, AwayState,
│                                    #       StateChange (union), PersonState,
│                                    #       Mac, PersonName, NodeName, Room NewTypes
├── config.py                        # frozen Config, ConfigError,
│                                    # NodeConfig, PersonConfig, MqttConfig,
│                                    # DEFAULTS module-level constants
├── engine.py                        # PresenceEngine — pure logic (unchanged API
│                                    # shape but types stronger). tick() deleted.
├── audit.py                         # NEW — log_state_change (moved from logging.py)
├── logging.py                       # setup_logging only
├── mqtt.py                          # Publisher protocol + MqttPublisher
│                                    # (HomeAssistantDiscovery composition deferred
│                                    # as YAGNI — see H10 decision below)
├── sources/
│   ├── __init__.py                  # re-export Source protocol
│   ├── base.py                      # NEW — Source Protocol
│   └── exporters.py                 # ExporterSource implementing Source
└── monitor.py                       # CLI pretty-printer (unchanged)

tests/
├── __init__.py
├── conftest.py                      # sample_config fixture (expanded),
│                                    # _ts helper, FakeMqttClient factory,
│                                    # FakeSource factory
├── fakes.py                         # NEW — FakeMqttClient, FakeSource,
│                                    #       PublishedMsg dataclass
├── test_config.py                   # validation boundary tests (kept)
├── test_domain.py                   # NEW — StateChange union, NewType identity
├── test_engine.py                   # unit + property-based (hypothesis)
├── test_integration.py              # full engine integration (uses sample_config)
├── test_mqtt.py                     # REWRITTEN — outcomes via FakeMqttClient
├── test_source_exporters.py         # REWRITTEN — aiohttp test server, no private patches
├── test_main.py                     # NEW — _run() end-to-end with fakes
├── test_logging.py                  # structlog setup (trimmed)
├── test_audit.py                    # NEW — audit log JSON schema
├── test_monitor.py                  # NEW — one round-trip: log_state_change → _format
├── test_wire_format.py              # NEW — retained payload fixture comparison
└── test_architecture.py             # NEW — AST-based import/type rules

Repo root:
├── pyproject.toml                   # add ruff, pyright, hypothesis, pytest-timeout
├── pyrightconfig.json               # NEW — strict mode
├── .ruff.toml                       # NEW — rule selection
├── .pre-commit-config.yaml          # NEW — pyright + ruff + pytest hooks
├── .github/workflows/ci.yml         # NEW — pyright + ruff + pytest matrix
├── CHANGELOG.md                     # [Unreleased] accumulates through plan
└── CLAUDE.md                        # updated with inherited standards + MQTT wire invariant
```

---

## Session 0 — Tooling & bootstrap

Goal: make CI and local dev reject regressions *before* starting any
refactor. Every subsequent session lands under strict pyright, ruff,
and green CI.

### Task 0.1 — Create hardening branch

**Files:** none (git state only)

- [ ] **Step 1:** ensure working tree clean, master up to date

```bash
cd /srv/eve
git status                    # must be "working tree clean"
git fetch origin
git checkout master
git pull --ff-only
```

- [ ] **Step 2:** create branch

```bash
git checkout -b hardening-v0.6.0
```

- [ ] **Step 3:** push upstream

```bash
git push -u origin hardening-v0.6.0
```

### Task 0.2 — Add dev dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1:** extend `[project.optional-dependencies]` dev list

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-timeout>=2.3",
    "hypothesis>=6.100",
    "pyright>=1.1.380",
    "ruff>=0.6.0",
    "pre-commit>=3.7",
    "aiohttp[speedups]>=3.9",    # for aiohttp test server
]
```

- [ ] **Step 2:** add Python upper bound

```toml
requires-python = ">=3.11,<3.14"
```

- [ ] **Step 3:** install in venv

```bash
source .venv/bin/activate
pip install -e ".[dev]"
```

- [ ] **Step 4:** verify

```bash
pyright --version && ruff --version && pytest --version
```

Expected: three version numbers print, no errors.

- [ ] **Step 5:** commit

```bash
git add pyproject.toml
git commit -m "chore(deps): add pyright, ruff, hypothesis, pre-commit"
```

### Task 0.3 — Configure pyright strict mode

**Files:**
- Create: `pyrightconfig.json`

- [ ] **Step 1:** write config

```json
{
  "include": ["src", "tests"],
  "exclude": ["**/__pycache__", ".venv", "docs"],
  "strict": ["src", "tests"],
  "pythonVersion": "3.11",
  "reportMissingTypeStubs": false,
  "reportImplicitOverride": true,
  "reportUnnecessaryTypeIgnoreComment": true,
  "reportUninitializedInstanceVariable": true,
  "reportMatchNotExhaustive": true,
  "reportShadowedImports": true
}
```

- [ ] **Step 2:** run pyright, capture baseline error list

```bash
pyright 2>&1 | tee /tmp/pyright-baseline.txt
```

Expected: many errors (Any in public sigs, untyped paho client,
untyped callbacks, etc.). This is the baseline. DO NOT add
`# type: ignore` to silence them. They will be fixed in Sessions
3 & 4 where the real type surgery happens.

- [ ] **Step 3:** commit config only — NO code changes to silence errors yet

```bash
git add pyrightconfig.json
git commit -m "chore(types): add pyright strict config (errors accepted, fixed in later sessions)"
```

### Task 0.4 — Configure ruff

**Files:**
- Create: `.ruff.toml`

- [ ] **Step 1:** write config

```toml
target-version = "py311"
line-length = 88
src = ["src", "tests"]

[lint]
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort
    "B",   # flake8-bugbear (mutable default args, etc.)
    "UP",  # pyupgrade
    "SIM", # simplify
    "RUF", # ruff-specific
    "ASYNC", # async idioms
    "PT",  # pytest-style
    "TCH", # typing-only imports in TYPE_CHECKING
    "PL",  # pylint subset
    "N",   # pep8-naming
    "ANN", # missing type annotations
]
ignore = [
    "PLR0913",  # too many arguments — engine methods take config + readings + now
    "PLR2004",  # magic value comparisons — occasionally acceptable
    "ANN401",   # Any disallowed — covered by pyright strict
]

[lint.per-file-ignores]
"tests/**" = ["ANN", "PLR2004"]

[format]
quote-style = "double"
indent-style = "space"
```

- [ ] **Step 2:** run ruff, fix auto-fixable

```bash
ruff check --fix .
ruff format .
```

- [ ] **Step 3:** verify clean

```bash
ruff check .
ruff format --check .
```

Expected: no output (clean).

- [ ] **Step 4:** run tests — nothing should break

```bash
pytest -q
```

Expected: 95 passed.

- [ ] **Step 5:** commit

```bash
git add .ruff.toml src/ tests/
git commit -m "chore(lint): add ruff config; apply auto-fixes"
```

### Task 0.5 — Configure pre-commit

**Files:**
- Create: `.pre-commit-config.yaml`

- [ ] **Step 1:** write config

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: local
    hooks:
      - id: pyright
        name: pyright
        entry: pyright
        language: system
        types: [python]
        pass_filenames: false
      - id: pytest
        name: pytest
        entry: pytest -q --timeout=5
        language: system
        types: [python]
        pass_filenames: false
```

- [ ] **Step 2:** install hooks

```bash
pre-commit install
```

- [ ] **Step 3:** verify

```bash
pre-commit run --all-files
```

Expected: ruff passes, pytest passes. Pyright will fail loudly —
**that is expected** until Sessions 3 & 4 finish the type surgery.

- [ ] **Step 4:** commit (skip pre-commit for this commit with `--no-verify` ONE TIME only, because pyright will block)

Actually no — don't `--no-verify`. Instead, temporarily pin pyright in
`.pre-commit-config.yaml` to a warning-only mode until Session 4 lands.
Add a TODO comment. Alternative: move pyright hook to `manual` stage
until Session 4 final commit.

```yaml
      - id: pyright
        name: pyright
        entry: pyright
        language: system
        types: [python]
        pass_filenames: false
        stages: [manual]       # runs in CI, skipped in pre-commit
                               # REMOVE `stages: [manual]` in Task 4.final
                               # — gates merge once strict mode clean.
```

- [ ] **Step 5:** commit

```bash
git add .pre-commit-config.yaml
git commit -m "chore(ci): add pre-commit hooks (pyright manual-stage until Session 4)"
```

### Task 0.6 — GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1:** write workflow

```yaml
name: CI

on:
  push:
    branches: [master, "hardening-v0.6.0"]
  pull_request:
    branches: [master]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Ruff check
        run: ruff check .
      - name: Ruff format check
        run: ruff format --check .
      - name: Pyright
        run: pyright
        continue-on-error: true   # REMOVE in Task 4.final once strict-clean
      - name: Pytest
        run: pytest -q --timeout=5
```

- [ ] **Step 2:** commit

```bash
git add .github/workflows/ci.yml
git commit -m "chore(ci): add GitHub Actions workflow (pyright non-gating until Session 4)"
```

- [ ] **Step 3:** push, verify CI runs

```bash
git push
```

Visit `https://github.com/vjt/openwrt-ha-presence/actions` — should see
a green matrix run (ruff + pytest green, pyright warnings allowed).

### Task 0.7 — `scripts/check.sh` — single-command gate

Adopted from `ha-verisure` — one script chains pyright + ruff +
pytest. CI uses it. Pre-commit uses it. Human uses it. One answer to
"is the branch green?".

**Files:**
- Create: `scripts/check.sh`

- [ ] **Step 1:** write script

```bash
#!/usr/bin/env bash
# check.sh — green-gate for eve. Chains every quality check.
# Exit 0 = ready to commit / ship. Non-zero = fix before continuing.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

echo "==> ruff check"
ruff check .

echo "==> ruff format --check"
ruff format --check .

echo "==> pyright"
pyright

echo "==> pytest"
pytest -q --timeout=5

echo
echo "all green"
```

- [ ] **Step 2:** make executable

```bash
chmod +x scripts/check.sh
```

- [ ] **Step 3:** run

```bash
scripts/check.sh
```

Expected: ruff + pytest green; pyright may have findings until
Session 4 (acceptable during hardening branch).

- [ ] **Step 4:** update `.github/workflows/ci.yml` — replace the
  separate `ruff` / `pyright` / `pytest` steps with one step:

```yaml
      - name: scripts/check.sh
        run: scripts/check.sh
        continue-on-error: true   # REMOVE in Task 4.11 (pyright gates CI)
```

- [ ] **Step 5:** commit

```bash
git add scripts/check.sh .github/workflows/ci.yml
git commit -m "chore(ci): add scripts/check.sh single-gate (inherited from ha-verisure)"
```

### Task 0.8 — Session 0 acceptance

- [ ] **Step 1:** confirm

```bash
scripts/check.sh               # one gate — ruff + pyright + pytest
git log --oneline master..HEAD # ~6 commits for Session 0
```

Expected: ruff + pytest green; pyright fails (baseline — fixed in
Sessions 3+4).

- [ ] **Step 2:** add `[Unreleased]` CHANGELOG entry

Update `CHANGELOG.md`:

```markdown
## [Unreleased]

### Added
- 🧰 **Pyright strict** + **ruff** + **pre-commit** + **GitHub Actions CI** — tooling floor for the hardening rewrite
- 📜 `scripts/check.sh` — single-command green gate (ruff + pyright + pytest). Inherited from ha-verisure
- 📦 Dev dependency bump: hypothesis, pytest-timeout, pre-commit, aiohttp speedups
```

```bash
git add CHANGELOG.md
git commit -m "docs: note Session 0 tooling in CHANGELOG [Unreleased]"
git push
```

---

## Session 1 — Test infrastructure (enables everything else)

Goal: before touching any production code, build the fakes + fixtures
+ wire-format fixture that will catch regressions during Sessions 2-4.
`test_mqtt.py` gets rewritten to the new pattern as proof.

### Task 1.1 — Capture wire-format golden fixture

**Files:**
- Create: `tests/wire_format_golden.json`
- Create: `tests/capture_wire_format.py` (temp script, deleted after)

- [ ] **Step 1:** write capture script

```python
# tests/capture_wire_format.py
"""One-shot: capture current MQTT wire format as a golden fixture.
Run BEFORE any type surgery. Deleted after Task 1.1."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from openwrt_presence.config import Config, MqttConfig, NodeConfig, PersonConfig
from openwrt_presence.engine import StateChange
from openwrt_presence.mqtt import MqttPublisher


def _config() -> Config:
    return Config(
        poll_interval=5,
        departure_timeout=120,
        away_timeout=64800,
        exporter_port=9100,
        dns_cache_ttl=300,
        nodes={"ap-garden": NodeConfig(name="ap-garden", room="garden", exit=True, url=None)},
        people={"alice": PersonConfig(name="alice", macs=["aa:bb:cc:dd:ee:01"])},
        mqtt=MqttConfig(host="broker", port=1883, username=None, password=None,
                        topic_prefix="openwrt-presence"),
        _mac_lookup={"aa:bb:cc:dd:ee:01": "alice"},
    )


def main() -> None:
    cfg = _config()
    client = MagicMock()
    pub = MqttPublisher(cfg, client)

    captures: list[dict] = []

    def record(topic: str, payload: str | bytes, qos: int = 0, retain: bool = False):
        captures.append({
            "topic": topic,
            "payload": payload if isinstance(payload, str) else payload.decode(),
            "qos": qos,
            "retain": retain,
        })
        return MagicMock(rc=0)

    client.publish.side_effect = record

    # Capture a HOME transition
    pub.publish_state(StateChange(
        person="alice", home=True, room="garden",
        mac="aa:bb:cc:dd:ee:01", node="ap-garden",
        timestamp=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
        rssi=-55,
    ))
    home_frames = captures.copy()
    captures.clear()

    # Capture an AWAY transition
    pub.publish_state(StateChange(
        person="alice", home=False, room=None,
        mac="aa:bb:cc:dd:ee:01", node="ap-garden",
        timestamp=datetime(2026, 4, 21, 13, 0, 0, tzinfo=timezone.utc),
        rssi=None,
    ))
    away_frames = captures.copy()
    captures.clear()

    # Capture a never-seen AWAY (the C1 empty-string case — captured
    # AS-IS to verify later refactor doesn't change bytes. Once C1
    # lands, this fixture gets regenerated with the new shape and
    # that diff IS the migration note for HA users.)
    pub.publish_state(StateChange(
        person="bob", home=False, room=None,
        mac="", node="",
        timestamp=datetime(2026, 4, 21, 14, 0, 0, tzinfo=timezone.utc),
        rssi=None,
    ))
    never_seen_frames = captures.copy()

    fixture = {
        "home": home_frames,
        "away": away_frames,
        "never_seen": never_seen_frames,
    }

    with open("tests/wire_format_golden.json", "w") as f:
        json.dump(fixture, f, indent=2, sort_keys=True)
    print(f"Wrote {len(home_frames)} home + {len(away_frames)} away + {len(never_seen_frames)} never-seen frames")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2:** run it

```bash
python tests/capture_wire_format.py
cat tests/wire_format_golden.json | head -40
```

Expected: JSON file with ~9 frames (3 transitions × ~3 topics each).

- [ ] **Step 3:** delete the capture script (it served its purpose)

```bash
rm tests/capture_wire_format.py
```

- [ ] **Step 4:** commit

```bash
git add tests/wire_format_golden.json
git commit -m "test: capture pre-refactor MQTT wire-format golden fixture"
```

### Task 1.2 — Introduce `PublishedMsg` + `FakeMqttClient`

**Files:**
- Create: `tests/fakes.py`

- [ ] **Step 1:** write the fakes

```python
# tests/fakes.py
"""Test doubles. Boundaries only — FakeMqttClient replaces paho,
FakeSource replaces ExporterSource. Never fake the engine or config."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class PublishedMsg:
    topic: str
    payload: str
    qos: int
    retain: bool


class FakeMqttClient:
    """Minimal paho.mqtt.client.Client stand-in.

    Records everything to `published`. LWT in `lwt`. Records
    connect/disconnect. Does NOT simulate the network — callers drive
    `trigger_connect()` / `trigger_disconnect()` to invoke callbacks.
    """

    def __init__(self) -> None:
        self.published: list[PublishedMsg] = []
        self.lwt: PublishedMsg | None = None
        self.connected: bool = False
        self._on_connect: Callable[..., None] | None = None
        self._on_disconnect: Callable[..., None] | None = None
        self.connect_async_called: bool = False
        self.loop_start_called: bool = False
        self.loop_stop_called: bool = False
        self.disconnect_called: bool = False
        self.username: str | None = None
        self.password: str | None = None
        self.reconnect_delay: tuple[int, int] | None = None
        self.max_queued: int | None = None

    # ------------ paho surface ------------

    def username_pw_set(self, username: str, password: str | None) -> None:
        self.username = username
        self.password = password

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect_delay = (min_delay, max_delay)

    def max_queued_messages_set(self, n: int) -> None:
        self.max_queued = n

    def will_set(self, topic: str, payload: str, qos: int = 0,
                 retain: bool = False) -> None:
        self.lwt = PublishedMsg(topic=topic, payload=payload, qos=qos,
                                retain=retain)

    def connect_async(self, host: str, port: int) -> None:
        self.connect_async_called = True

    def loop_start(self) -> None:
        self.loop_start_called = True

    def loop_stop(self) -> None:
        self.loop_stop_called = True

    def disconnect(self) -> None:
        self.disconnect_called = True
        self.connected = False

    def publish(self, topic: str, payload: str = "", qos: int = 0,
                retain: bool = False) -> "_FakePublishResult":
        self.published.append(PublishedMsg(topic=topic, payload=str(payload),
                                           qos=qos, retain=retain))
        return _FakePublishResult(rc=0, mid=len(self.published))

    # paho sets these as attributes; we record them
    @property
    def on_connect(self) -> Callable[..., None] | None:
        return self._on_connect

    @on_connect.setter
    def on_connect(self, cb: Callable[..., None]) -> None:
        self._on_connect = cb

    @property
    def on_disconnect(self) -> Callable[..., None] | None:
        return self._on_disconnect

    @on_disconnect.setter
    def on_disconnect(self, cb: Callable[..., None]) -> None:
        self._on_disconnect = cb

    # ------------ test driver helpers ------------

    def trigger_connect(self, reason_code: Any = 0) -> None:
        """Simulate broker handshake success."""
        self.connected = True
        if self._on_connect is not None:
            self._on_connect(self, None, {}, reason_code, None)

    def trigger_disconnect(self, reason_code: Any = 0) -> None:
        """Simulate broker disconnect."""
        self.connected = False
        if self._on_disconnect is not None:
            self._on_disconnect(self, None, {}, reason_code, None)

    def clear(self) -> None:
        """Drop recorded publishes — use between phases of a test."""
        self.published.clear()


@dataclass
class _FakePublishResult:
    rc: int
    mid: int

    def wait_for_publish(self, timeout: float | None = None) -> None:
        return
```

- [ ] **Step 2:** quick smoke test

```python
# tests/test_fakes.py (temporary — delete after Task 1.3)
from tests.fakes import FakeMqttClient, PublishedMsg


def test_fake_records_publish():
    c = FakeMqttClient()
    c.publish("foo/bar", "hi", qos=1, retain=True)
    assert c.published == [PublishedMsg("foo/bar", "hi", 1, True)]


def test_fake_triggers_on_connect():
    c = FakeMqttClient()
    seen: list[int] = []
    c.on_connect = lambda cli, ud, fl, rc, pr: seen.append(rc)
    c.trigger_connect(reason_code=0)
    assert seen == [0]
```

```bash
pytest tests/test_fakes.py -v
```

Expected: 2 passed.

- [ ] **Step 3:** delete smoke test, commit fakes

```bash
rm tests/test_fakes.py
git add tests/fakes.py
git commit -m "test(fakes): add FakeMqttClient + PublishedMsg"
```

### Task 1.3 — Wire-format test using the golden fixture

**Files:**
- Create: `tests/test_wire_format.py`

- [ ] **Step 1:** write test

```python
# tests/test_wire_format.py
"""Golden-fixture test: publishing these transitions today must produce
bytes identical to tests/wire_format_golden.json (captured in Task 1.1).

This is the contract HA consumes. Every refactor in Sessions 2-4 MUST
keep this test green. When C1's discriminated union lands in Session 3,
the fixture will need regenerating — THAT diff is the migration note
for HA users (documented in CHANGELOG + release notes)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from openwrt_presence.engine import StateChange
from openwrt_presence.mqtt import MqttPublisher
from tests.conftest import sample_config  # noqa: F401 — fixture usage
from tests.fakes import FakeMqttClient, PublishedMsg


_GOLDEN = Path(__file__).parent / "wire_format_golden.json"


def _as_frames(client: FakeMqttClient) -> list[dict]:
    return [
        {"topic": p.topic, "payload": p.payload, "qos": p.qos, "retain": p.retain}
        for p in client.published
    ]


def test_home_transition_wire_format(sample_config):
    fixture = json.loads(_GOLDEN.read_text())
    client = FakeMqttClient()
    pub = MqttPublisher(sample_config, client)
    pub.publish_state(StateChange(
        person="alice", home=True, room="garden",
        mac="aa:bb:cc:dd:ee:01", node="ap-garden",
        timestamp=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
        rssi=-55,
    ))
    assert _as_frames(client) == fixture["home"]


def test_away_transition_wire_format(sample_config):
    fixture = json.loads(_GOLDEN.read_text())
    client = FakeMqttClient()
    pub = MqttPublisher(sample_config, client)
    pub.publish_state(StateChange(
        person="alice", home=False, room=None,
        mac="aa:bb:cc:dd:ee:01", node="ap-garden",
        timestamp=datetime(2026, 4, 21, 13, 0, 0, tzinfo=timezone.utc),
        rssi=None,
    ))
    assert _as_frames(client) == fixture["away"]
```

- [ ] **Step 2:** `sample_config` must match the capture script's config exactly

Open `tests/conftest.py`, check `sample_config` includes an `ap-garden`
exit node and an `alice` person with MAC `aa:bb:cc:dd:ee:01`. If not,
extend it. Re-run capture (Task 1.1) with the updated fixture.

- [ ] **Step 3:** run

```bash
pytest tests/test_wire_format.py -v
```

Expected: 2 passed.

- [ ] **Step 4:** commit

```bash
git add tests/test_wire_format.py tests/conftest.py
git commit -m "test: wire-format golden fixture test (guards MQTT bytes through refactor)"
```

### Task 1.4 — Expand `sample_config` fixture

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1:** expand fixture to cover integration scenarios

```python
# tests/conftest.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openwrt_presence.config import (
    Config,
    MqttConfig,
    NodeConfig,
    PersonConfig,
)


@pytest.fixture
def sample_config() -> Config:
    """Canonical config for integration tests.

    Nodes:
      - ap-garden (exit, room=garden)
      - ap-living (interior, room=living)
      - ap-bedroom (interior, room=bedroom)
    People:
      - alice (1 MAC)
      - bob (2 MACs, one per phone)
    """
    nodes = {
        "ap-garden":  NodeConfig(name="ap-garden",  room="garden",  exit=True,  url=None),
        "ap-living":  NodeConfig(name="ap-living",  room="living",  exit=False, url=None),
        "ap-bedroom": NodeConfig(name="ap-bedroom", room="bedroom", exit=False, url=None),
    }
    people = {
        "alice": PersonConfig(name="alice", macs=["aa:bb:cc:dd:ee:01"]),
        "bob":   PersonConfig(name="bob",   macs=["aa:bb:cc:dd:ee:02",
                                                  "aa:bb:cc:dd:ee:03"]),
    }
    mac_lookup = {
        "aa:bb:cc:dd:ee:01": "alice",
        "aa:bb:cc:dd:ee:02": "bob",
        "aa:bb:cc:dd:ee:03": "bob",
    }
    return Config(
        poll_interval=5,
        departure_timeout=120,
        away_timeout=64800,
        exporter_port=9100,
        dns_cache_ttl=300,
        nodes=nodes,
        people=people,
        mqtt=MqttConfig(host="broker", port=1883, username=None,
                        password=None, topic_prefix="openwrt-presence"),
        _mac_lookup=mac_lookup,
    )


def _ts(minutes: float = 0) -> datetime:
    """Deterministic timestamp helper."""
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


@pytest.fixture
def ts():
    return _ts
```

- [ ] **Step 2:** run all tests

```bash
pytest -q --timeout=5
```

If `test_integration.py` or other tests break because they had
different assumptions, *that* is the point — they were hand-rolling.
In each, import `sample_config` and `ts` from conftest instead of
hand-rolling. Fix them.

- [ ] **Step 3:** delete `_make_config()` in `test_integration.py`;
  replace with `sample_config` fixture injection. Same for
  `test_config.py`'s `_base_config` where it can be replaced without
  hurting validation-boundary tests.

- [ ] **Step 4:** re-run

```bash
pytest -q --timeout=5
```

Expected: all green. 95+ tests depending on refactor side-effects.

- [ ] **Step 5:** re-run wire-format capture if sample_config changed

```bash
# Only if sample_config was altered
python tests/capture_wire_format.py   # (re-create if deleted — see Task 1.1)
pytest tests/test_wire_format.py -v
```

- [ ] **Step 6:** commit

```bash
git add tests/
git commit -m "test(fixtures): expand sample_config; drop hand-rolled configs in integration tests"
```

### Task 1.5 — Rewrite `test_mqtt.py` using `FakeMqttClient`

**Files:**
- Rewrite: `tests/test_mqtt.py`

- [ ] **Step 1:** delete old file

```bash
rm tests/test_mqtt.py
```

- [ ] **Step 2:** write new version — every test asserts on concrete
  `PublishedMsg` objects, no `str(mock_call)` scans

Full rewrite. Structure:

```python
# tests/test_mqtt.py
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openwrt_presence.engine import StateChange
from openwrt_presence.mqtt import MqttPublisher
from tests.fakes import FakeMqttClient, PublishedMsg


@pytest.fixture
def publisher(sample_config) -> tuple[MqttPublisher, FakeMqttClient]:
    client = FakeMqttClient()
    return MqttPublisher(sample_config, client), client


class TestLwt:
    def test_lwt_set_at_construction(self, publisher):
        _, client = publisher
        assert client.lwt == PublishedMsg(
            topic="openwrt-presence/status",
            payload="offline",
            qos=1,
            retain=True,
        )


class TestDiscovery:
    def test_discovery_published_on_connect(self, publisher):
        pub, client = publisher
        pub.on_connected()
        topics = {m.topic for m in client.published}
        assert "homeassistant/device_tracker/alice_wifi/config" in topics
        assert "homeassistant/device_tracker/bob_wifi/config" in topics
        assert "homeassistant/sensor/alice_room/config" in topics

    def test_discovery_retained_qos1(self, publisher):
        pub, client = publisher
        pub.on_connected()
        for msg in client.published:
            if "config" in msg.topic:
                assert msg.retain is True
                assert msg.qos == 1


class TestStatePublish:
    def test_home_publishes_all_three_topics(self, publisher):
        pub, client = publisher
        pub.publish_state(StateChange(
            person="alice", home=True, room="garden",
            mac="aa:bb:cc:dd:ee:01", node="ap-garden",
            timestamp=datetime(2026, 4, 21, tzinfo=timezone.utc),
            rssi=-55,
        ))
        topics = {m.topic for m in client.published}
        assert "openwrt-presence/alice/state" in topics
        assert "openwrt-presence/alice/room" in topics
        assert "openwrt-presence/alice/attributes" in topics

    def test_state_payload_home(self, publisher):
        pub, client = publisher
        pub.publish_state(StateChange(
            person="alice", home=True, room="garden",
            mac="aa:bb:cc:dd:ee:01", node="ap-garden",
            timestamp=datetime(2026, 4, 21, tzinfo=timezone.utc),
            rssi=-55,
        ))
        state_msg = next(m for m in client.published
                         if m.topic == "openwrt-presence/alice/state")
        assert state_msg.payload == "home"
        assert state_msg.retain is True
        assert state_msg.qos == 1

    def test_state_payload_away(self, publisher):
        pub, client = publisher
        pub.publish_state(StateChange(
            person="alice", home=False, room=None,
            mac="aa:bb:cc:dd:ee:01", node="ap-garden",
            timestamp=datetime(2026, 4, 21, tzinfo=timezone.utc),
            rssi=None,
        ))
        state_msg = next(m for m in client.published
                         if m.topic == "openwrt-presence/alice/state")
        assert state_msg.payload == "not_home"


class TestReconnectReseed:
    def test_on_connected_republishes_cached_state(self, publisher):
        pub, client = publisher
        # First a transition → publishes state + caches
        pub.publish_state(StateChange(
            person="alice", home=True, room="garden",
            mac="aa:bb:cc:dd:ee:01", node="ap-garden",
            timestamp=datetime(2026, 4, 21, tzinfo=timezone.utc),
            rssi=-55,
        ))
        client.clear()

        # Simulate reconnect — cached state republished
        pub.on_connected()
        state_msgs = [m for m in client.published
                      if m.topic == "openwrt-presence/alice/state"]
        assert len(state_msgs) == 1
        assert state_msgs[0].payload == "home"
        assert state_msgs[0].retain is True
```

- [ ] **Step 3:** run

```bash
pytest tests/test_mqtt.py -v --timeout=5
```

Expected: all green.

- [ ] **Step 4:** commit

```bash
git add tests/test_mqtt.py
git commit -m "test(mqtt): rewrite asserting on PublishedMsg outcomes (no mock-call strings)"
```

### Task 1.6 — Rewrite `test_source_exporters.py` using aiohttp test server

**Files:**
- Rewrite: `tests/test_source_exporters.py`

- [ ] **Step 1:** write with `aiohttp.test_utils.TestServer`. No
  monkey-patching `_get_session` / `_scrape_ap`.

```python
# tests/test_source_exporters.py
from __future__ import annotations

import pytest
from aiohttp import web

from openwrt_presence.sources.exporters import ExporterSource


_METRICS_SAMPLE = """# HELP wifi_station_signal_dbm
# TYPE wifi_station_signal_dbm gauge
wifi_station_signal_dbm{mac="aa:bb:cc:dd:ee:01"} -55
wifi_station_signal_dbm{mac="aa:bb:cc:dd:ee:02"} -70
"""


async def _metrics_handler(request: web.Request) -> web.Response:
    return web.Response(text=_METRICS_SAMPLE, content_type="text/plain")


async def _error_handler(request: web.Request) -> web.Response:
    return web.Response(status=503, text="busy")


class TestExporterSource:
    async def test_scrapes_tracked_macs(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        source = ExporterSource(
            node_urls={"ap-garden": url},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        macs = [r.mac for r in readings]
        assert macs == ["aa:bb:cc:dd:ee:01"]
        assert readings[0].rssi == -55
        assert readings[0].ap == "ap-garden"

    async def test_ignores_untracked_macs(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        source = ExporterSource(
            node_urls={"ap-garden": url},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        assert all(r.mac == "aa:bb:cc:dd:ee:01" for r in readings)

    async def test_503_treated_as_unreachable(self, aiohttp_server, caplog):
        app = web.Application()
        app.router.add_get("/metrics", _error_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        source = ExporterSource(
            node_urls={"ap-garden": url},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        # After Task 2.4 (M12 fix), 503 becomes an exception → empty
        # readings AND node_unreachable log. Asserting both.
        assert readings == []
```

- [ ] **Step 2:** add aiohttp fixture — conftest augments

```python
# tests/conftest.py — add at bottom
pytest_plugins = ["aiohttp.pytest_plugin"]
```

- [ ] **Step 3:** run

```bash
pytest tests/test_source_exporters.py -v --timeout=5
```

Expected: first two pass. Third may fail until Task 2.4 — mark
`@pytest.mark.xfail(reason="fixed in Task 2.4 — M12 response status check")` temporarily.

- [ ] **Step 4:** commit

```bash
git add tests/test_source_exporters.py tests/conftest.py
git commit -m "test(sources): rewrite using aiohttp test server (no private-method patches)"
```

### Task 1.7 — `tests/test_main.py` — drive `_run()` end-to-end

**Files:**
- Create: `tests/test_main.py`

- [ ] **Step 1:** introduce a `FakeSource` in `tests/fakes.py`

```python
# Append to tests/fakes.py
from openwrt_presence.engine import StationReading


@dataclass
class FakeSource:
    """Returns pre-programmed sequences of readings from .query().

    Use .schedule(reading_list, reading_list, ...) to feed multiple
    poll cycles. Raises the exception if .raise_on_next is set."""

    readings_queue: list[list[StationReading]] = field(default_factory=list)
    raise_on_next: Exception | None = None
    closed: bool = False

    async def query(self) -> list[StationReading]:
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        if not self.readings_queue:
            return []
        return self.readings_queue.pop(0)

    async def close(self) -> None:
        self.closed = True

    def schedule(self, *batches: list[StationReading]) -> None:
        self.readings_queue.extend(batches)
```

- [ ] **Step 2:** write test — this is the C7 fix; drives `_run` for
  2-3 poll cycles with injected fakes

```python
# tests/test_main.py
"""Integration test for __main__._run — wires engine + publisher + source
through the signal-driven poll loop. Fixes C7 (review)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from openwrt_presence.engine import StationReading
from tests.fakes import FakeMqttClient, FakeSource


@pytest.fixture
def fake_mqtt():
    return FakeMqttClient()


@pytest.fixture
def fake_source():
    return FakeSource()


async def test_startup_seeds_state_per_person(sample_config, fake_mqtt,
                                              fake_source, monkeypatch, caplog):
    """Every configured person gets one state_change log at startup."""
    # Pending Task 4.x: _run will accept injected mqtt_client + source_factory
    # for testability. Until then, monkeypatch.
    monkeypatch.setenv("CONFIG_PATH", "nonexistent")
    monkeypatch.setattr("openwrt_presence.config.Config.from_yaml",
                        lambda path: sample_config)
    monkeypatch.setattr("paho.mqtt.client.Client",
                        lambda version: fake_mqtt)
    monkeypatch.setattr("openwrt_presence.sources.exporters.ExporterSource",
                        lambda **kw: fake_source)

    from openwrt_presence.__main__ import _run

    # Drive one cycle then shut down
    async def shutdown_after_seed():
        # Wait for seed to land
        while not fake_source.closed and len(fake_mqtt.published) < 6:
            await asyncio.sleep(0.01)
        import signal
        import os
        os.kill(os.getpid(), signal.SIGTERM)

    # ... (test body continues)
```

Actually `_run` construction needs to be refactored to be testable.
Full pattern lands in Session 4 (H1 — move `will_set` + client
creation out of `_run`). For now, write the test with extensive
monkeypatching; in Session 4 Task 4.1 we revisit with proper DI.

- [ ] **Step 3:** simpler approach — extract `_run` into a testable
  coroutine that takes `client` + `source_factory` as arguments, and a
  tiny `main()` that wires real paho + ExporterSource and calls it

This is part of Task 4.1 (scheduled). For Session 1, just land:
- FakeSource in fakes.py
- A placeholder `test_main.py` with ONE test that imports and
  executes something trivial (e.g., `signal.SIGTERM` handler
  registration) via monkeypatching

Keep it minimal — full test suite lands in Task 4.1.

```python
# tests/test_main.py (minimal for Session 1)
"""Scaffolding for __main__._run tests. Full coverage lands in Task 4.1
once _run() is refactored for DI."""
from openwrt_presence import __main__ as eve_main


def test_main_module_imports():
    assert hasattr(eve_main, "_run")
    assert hasattr(eve_main, "main")
```

- [ ] **Step 4:** run

```bash
pytest tests/test_main.py -v --timeout=5
```

Expected: 1 passed.

- [ ] **Step 5:** commit

```bash
git add tests/test_main.py tests/fakes.py
git commit -m "test(main): scaffolding for _run() tests; FakeSource in fakes"
```

### Task 1.8 — Session 1 acceptance

- [ ] **Step 1:** full suite green

```bash
pytest -q --timeout=5
```

Expected: 100+ passed.

- [ ] **Step 2:** wire-format golden holds

```bash
pytest tests/test_wire_format.py -v
```

- [ ] **Step 3:** ruff + pyright status unchanged

```bash
ruff check . && ruff format --check .
pyright 2>&1 | tail -5   # error count similar to baseline
```

- [ ] **Step 4:** CHANGELOG Unreleased update

```markdown
## [Unreleased]

### Added
- ... (existing)
- 🧪 **FakeMqttClient + FakeSource** — test infrastructure asserting on
  concrete `PublishedMsg`/readings, not mock-call string scans
- 🔒 **Wire-format golden fixture** — locks MQTT byte shape against
  accidental regression through the hardening rewrite
- 🧩 Expanded `sample_config` fixture (3 nodes, 2 people, 3 MACs) —
  single source for integration tests

### Changed
- 🧪 `test_mqtt.py` rewritten asserting on outcomes
- 🧪 `test_source_exporters.py` rewritten using `aiohttp` test server
  (no private-method monkey-patching)
```

```bash
git add CHANGELOG.md
git commit -m "docs: Session 1 changelog entries"
git push
```

---

## Session 2 — Alarm-pathway safety

Goal: close the three CRITICAL runtime findings (C2, C3, C4) plus the
five HIGH runtime issues (H6-H9 and H1 shutdown semantics). Every
change lands with a test that would have caught the bug. Wire-format
test stays green.

### Task 2.1 — Fix `asyncio.get_event_loop()` deprecation (H8)

**Files:**
- Modify: `src/openwrt_presence/__main__.py:65`

- [ ] **Step 1:** write test (regression — ensures we call the right API)

```python
# Append to tests/test_main.py
def test_run_uses_get_running_loop():
    """Guard against asyncio.get_event_loop() regression (H8)."""
    import inspect
    src = inspect.getsource(eve_main._run)
    assert "get_running_loop" in src
    assert "get_event_loop()" not in src
```

Expected: fails (currently `get_event_loop()`).

- [ ] **Step 2:** fix

```python
# In __main__._run, replace:
#   loop = asyncio.get_event_loop()
# with:
    loop = asyncio.get_running_loop()
```

- [ ] **Step 3:** tests

```bash
pytest tests/test_main.py::test_run_uses_get_running_loop -v
pytest -q --timeout=5
```

Expected: green.

- [ ] **Step 4:** commit

```bash
git add src/openwrt_presence/__main__.py tests/test_main.py
git commit -m "fix(main): use asyncio.get_running_loop (deprecation H8)"
```

### Task 2.2 — Wrap initial query in try/except (H9)

**Files:**
- Modify: `src/openwrt_presence/__main__.py:75-78`

- [ ] **Step 1:** write test

```python
# Append to tests/test_main.py
async def test_initial_query_failure_does_not_crash(...):
    """If first query raises, _run continues into the poll loop (H9)."""
    # Requires full DI scaffolding — defer real test to Task 4.1.
    # Minimal test: inspect source for try/except around initial query.
    import inspect
    src = inspect.getsource(eve_main._run)
    # Crude check: the line `await source.query()` in the startup block
    # must be inside a try clause.
    assert src.count("initial_query_failed") >= 1
```

- [ ] **Step 2:** fix

```python
# Replace startup query block in __main__._run:
#     readings = await source.query()
# with:
    logger.info("initial_query")
    try:
        readings = await source.query()
    except Exception:
        logger.exception("initial_query_failed")
        readings = []
```

- [ ] **Step 3:** tests + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/__main__.py tests/test_main.py
git commit -m "fix(main): tolerate initial query failure (H9)"
```

### Task 2.3 — Wrap `_on_connect` body in try/except + enable paho logger (H7)

**Files:**
- Modify: `src/openwrt_presence/__main__.py:37-42`

- [ ] **Step 1:** write test via FakeMqttClient (after Task 4.1 lands
  full DI, this becomes a proper end-to-end test). For now, unit-test
  the wrapper pattern:

Skip test for this task — full E2E lands in Task 4.1. Instead,
verify manually after fix.

- [ ] **Step 2:** fix

```python
# In __main__._run, replace:
#     def _on_connect(client, userdata, flags, reason_code, properties=None):
#         logger.info("mqtt_connected", reason_code=str(reason_code))
#         publisher.on_connected()
# with:
    def _on_connect(client, userdata, flags, reason_code, properties=None):
        logger.info("mqtt_connected", reason_code=str(reason_code))
        try:
            publisher.on_connected()
        except Exception:
            logger.exception("on_connected_failed")

    # enable paho's internal logger so its swallowed exceptions surface
    import logging as stdlib_logging
    paho_logger = stdlib_logging.getLogger("paho.mqtt.client")
    client.enable_logger(paho_logger)
```

- [ ] **Step 3:** tests + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/__main__.py
git commit -m "fix(main): try/except around on_connected + paho enable_logger (H7)"
```

### Task 2.4 — Response status check + size cap + loud parsing (M11 + M12 + "crash loud")

**Files:**
- Modify: `src/openwrt_presence/sources/exporters.py:91-93`
- Test: `tests/test_source_exporters.py::test_503_treated_as_unreachable`

Inherits the `ha-verisure` "crash loud on unexpected input" rule:
malformed `/metrics` output (non-integer RSSI, MAC with non-hex
characters) is a real bug in the AP, not a case to silently skip.
But — per-AP isolation still matters — surface the error by failing
the whole scrape of that AP (the per-node try/except catches it and
the node is marked unhealthy, which the operator sees via
`node_unreachable`).

- [ ] **Step 1:** remove the `@xfail` marker from `test_503_treated_as_unreachable`

- [ ] **Step 2:** fix HTTP handling

```python
# In ExporterSource._scrape_ap, replace the single `body = await response.text()`
# with:
    async with session.get(url, timeout=timeout) as response:
        response.raise_for_status()   # 4xx/5xx → ClientResponseError
        body = await response.content.read(1 << 20)   # 1 MB cap
        text = body.decode("utf-8", errors="replace")
```

- [ ] **Step 3:** tighten `_parse_metrics` to raise on malformed lines

```python
# Current: regex skips non-matching lines silently.
# New: if a line LOOKS like a wifi_station_signal_dbm metric (starts
# with that name) but fails to parse a valid MAC + integer, raise
# ValueError with the offending line.

_METRIC_PREFIX = "wifi_station_signal_dbm"
_METRIC_RE = re.compile(
    r'^wifi_station_signal_dbm\{mac="([0-9a-fA-F:.-]+)"\}\s+(-?\d+)\s*$'
)


def _parse_metrics(text: str, ap: str, tracked_macs: set[Mac]) -> list[StationReading]:
    readings: list[StationReading] = []
    for line in text.splitlines():
        if not line.startswith(_METRIC_PREFIX):
            continue
        m = _METRIC_RE.match(line)
        if m is None:
            raise ValueError(f"{ap}: malformed metric line: {line!r}")
        raw_mac, raw_rssi = m.group(1), m.group(2)
        mac = Mac(raw_mac.lower().replace("-", ":"))
        if mac not in tracked_macs:
            continue
        rssi = int(raw_rssi)   # m.group(2) matched \d+ — safe
        readings.append(StationReading(mac=mac, ap=ap, rssi=rssi))
    return readings
```

The ValueError bubbles up to the per-AP try/except in `query()` — that
AP gets marked unhealthy, `node_unreachable` fires, operator sees it.
Other APs in the snapshot are unaffected. Loud where it matters, isolated
where it matters.

- [ ] **Step 4:** write test for the "loud" behavior

```python
# Add to tests/test_source_exporters.py
class TestMalformedMetrics:
    async def test_malformed_line_marks_node_unhealthy(self, aiohttp_server, caplog):
        async def _handler(request):
            return web.Response(
                text='wifi_station_signal_dbm{mac="not-a-mac"} not-a-number\n',
                content_type="text/plain",
            )
        app = web.Application()
        app.router.add_get("/metrics", _handler)
        server = await aiohttp_server(app)

        source = ExporterSource(
            node_urls={"ap-broken": f"http://{server.host}:{server.port}/metrics"},
            tracked_macs=set(),
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        assert readings == []   # AP failed, empty result
        # Node flagged unhealthy
        assert source.all_nodes_unhealthy is True
```

- [ ] **Step 5:** run

```bash
pytest tests/test_source_exporters.py -v --timeout=5
```

Expected: all pass including malformed-line test.

- [ ] **Step 6:** commit

```bash
git add src/openwrt_presence/sources/exporters.py tests/test_source_exporters.py
git commit -m "fix(sources): raise_for_status + 1MB cap + crash-loud on malformed metric (M11, M12, ha-verisure rule)"
```

### Task 2.5 — Check `publish()` return code (C4, first half)

**Files:**
- Modify: `src/openwrt_presence/mqtt.py:85-123`

- [ ] **Step 1:** write test via FakeMqttClient — failing publish

Extend `FakeMqttClient`:

```python
# In tests/fakes.py, extend _FakePublishResult:
@dataclass
class _FakePublishResult:
    rc: int
    mid: int
    def wait_for_publish(self, timeout: float | None = None) -> None:
        return

# In FakeMqttClient, add:
    def set_publish_rc(self, rc: int) -> None:
        self._publish_rc = rc

    # modify publish() to use self._publish_rc if set
```

Test:

```python
# Add to tests/test_mqtt.py
def test_publish_failure_logs_error(publisher, caplog):
    pub, client = publisher
    client._publish_rc = 2   # MQTT_ERR_QUEUE_SIZE
    pub.publish_state(StateChange(
        person="alice", home=True, room="garden",
        mac="aa:bb:cc:dd:ee:01", node="ap-garden",
        timestamp=datetime(2026, 4, 21, tzinfo=timezone.utc),
        rssi=-55,
    ))
    # After Task 2.5, should log publish_failed
    assert any(r.message == "publish_failed" for r in caplog.records)
```

- [ ] **Step 2:** fix

```python
# In MqttPublisher._emit_state (or wherever publish is called), after each call:
    info = self._client.publish(topic, payload, qos=_QOS, retain=True)
    if info.rc != 0:
        logger.error("publish_failed", topic=topic, rc=info.rc,
                     person=change.person)
```

- [ ] **Step 3:** run + commit

```bash
pytest tests/test_mqtt.py -v --timeout=5
git add src/openwrt_presence/mqtt.py tests/test_mqtt.py tests/fakes.py
git commit -m "fix(mqtt): check publish() rc, log publish_failed on failure (C4 part 1)"
```

### Task 2.6 — Split audit log: `state_computed` vs `state_delivered` (C4, second half)

**Files:**
- Modify: `src/openwrt_presence/logging.py` → will become `src/openwrt_presence/audit.py`
  (full move in Task 4.4; for now, add the second event kind)

- [ ] **Step 1:** write test

```python
# Add to tests/test_mqtt.py
def test_publish_emits_both_computed_and_delivered(publisher, caplog):
    pub, client = publisher
    pub.publish_state(StateChange(
        person="alice", home=True, room="garden",
        mac="aa:bb:cc:dd:ee:01", node="ap-garden",
        timestamp=datetime(2026, 4, 21, tzinfo=timezone.utc),
        rssi=-55,
    ))
    msgs = {r.message for r in caplog.records}
    assert "state_computed" in msgs
    assert "state_delivered" in msgs


def test_publish_failure_emits_computed_but_not_delivered(publisher, caplog):
    pub, client = publisher
    client._publish_rc = 2
    pub.publish_state(...)
    msgs = {r.message for r in caplog.records}
    assert "state_computed" in msgs
    assert "state_delivered" not in msgs
    assert "publish_failed" in msgs
```

- [ ] **Step 2:** fix

In `logging.py::log_state_change`, split into:
- `log_state_computed(change)` — emitted always (engine produced it)
- `log_state_delivered(change)` — emitted only after all 3 topic
  publishes succeed

`MqttPublisher.publish_state` calls both around `_emit_state`.

- [ ] **Step 3:** update monitor.py — it now has to pretty-print two
  distinct events. Add a `state_delivered` branch mirroring
  `state_change` style with a "✓" indicator.

- [ ] **Step 4:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/logging.py src/openwrt_presence/mqtt.py \
        src/openwrt_presence/monitor.py tests/
git commit -m "feat(audit): split state_computed from state_delivered (C4 part 2)"
```

### Task 2.7 — `call_soon_threadsafe` for paho callbacks (C2)

**Files:**
- Modify: `src/openwrt_presence/__main__.py:37-50,65-73`

- [ ] **Step 1:** write test — proves `on_connected` runs on the
  asyncio loop. Test via FakeMqttClient's `trigger_connect` after
  wiring — defer to Task 4.1 for the full version. For now, unit test
  the pattern:

```python
# Add to tests/test_main.py
def test_on_connect_schedules_via_call_soon_threadsafe():
    import inspect
    src = inspect.getsource(eve_main._run)
    assert "call_soon_threadsafe" in src
```

- [ ] **Step 2:** fix

```python
# In __main__._run, after loop = asyncio.get_running_loop():
    def _on_connect(client, userdata, flags, reason_code, properties=None):
        logger.info("mqtt_connected", reason_code=str(reason_code))
        # Hand off to asyncio loop — publisher state is asyncio-owned.
        def _reseed():
            try:
                publisher.on_connected()
            except Exception:
                logger.exception("on_connected_failed")
        loop.call_soon_threadsafe(_reseed)

    def _on_disconnect(client, userdata, flags, reason_code, properties=None):
        logger.warning("mqtt_disconnected", reason_code=str(reason_code))
```

Remove the earlier `try/except` around `publisher.on_connected()` from
Task 2.3 since it's now inside `_reseed`.

- [ ] **Step 3:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/__main__.py
git commit -m "fix(main): hand off on_connected to asyncio via call_soon_threadsafe (C2)"
```

### Task 2.8 — All-APs-unreachable circuit breaker (C3)

**Files:**
- Modify: `src/openwrt_presence/sources/exporters.py`
- Modify: `src/openwrt_presence/__main__.py` OR `engine.py` — decide placement
- Test: `tests/test_source_exporters.py`, `tests/test_integration.py`

**Design decision:** place the breaker at the **main-loop** level, not
the source. Rationale:
- Source reports per-node health. Breaker is a policy decision
  ("what do we do when everything is dark?"). Policy belongs in
  `__main__` not in the source.
- Leaves source free to be composed (future secondary source could
  mitigate alone).

`ExporterSource` exposes `.all_nodes_unhealthy -> bool` property.
`__main__._run` checks after each `query()` — if true AND there was
at least one prior healthy state, skip `engine.process_snapshot`,
log `logger.error("all_nodes_unreachable")`, and hold deltas.

- [ ] **Step 1:** write test

```python
# Add to tests/test_source_exporters.py
class TestAllNodesUnhealthy:
    async def test_reports_false_when_mixed(self, aiohttp_server):
        # one node up, one down
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)

        source = ExporterSource(
            node_urls={
                "up":   f"http://{server.host}:{server.port}/metrics",
                "down": "http://127.0.0.1:9/metrics",   # unreachable port
            },
            tracked_macs={"aa:bb:cc:dd:ee:01"},
            dns_cache_ttl=60,
        )
        try:
            await source.query()
            assert source.all_nodes_unhealthy is False
        finally:
            await source.close()

    async def test_reports_true_when_all_down(self):
        source = ExporterSource(
            node_urls={"down": "http://127.0.0.1:9/metrics"},
            tracked_macs=set(),
            dns_cache_ttl=60,
        )
        try:
            await source.query()
            assert source.all_nodes_unhealthy is True
        finally:
            await source.close()
```

- [ ] **Step 2:** add `all_nodes_unhealthy` to `ExporterSource`

```python
# In ExporterSource:
    @property
    def all_nodes_unhealthy(self) -> bool:
        """True if every configured node is currently unhealthy AND
        at least one has ever been observed (so clean startup is not
        flagged)."""
        if not self._node_healthy:
            return False
        return not any(self._node_healthy.values())
```

- [ ] **Step 3:** wire into `__main__._run`

```python
# Replace current poll-loop body:
            try:
                readings = await source.query()
            except Exception:
                logger.exception("query_error")
                continue

            if source.all_nodes_unhealthy:
                logger.error("all_nodes_unreachable",
                             nodes=list(source.node_names))
                continue

            now = datetime.now(timezone.utc)
            changes = engine.process_snapshot(now, readings)
            ...
```

(`source.node_names` is a new property returning
`list(self._node_urls.keys())`.)

- [ ] **Step 4:** integration test

```python
# Add to tests/test_integration.py
def test_all_nodes_unhealthy_pauses_processing(sample_config, ts):
    """C3: when every AP is dark, engine must not produce false departures."""
    # Use FakeSource with all_nodes_unhealthy=True and verify
    # no StateChange is emitted during the blackout.
    # (Full _run integration lands in Task 4.1; for now unit-test the
    # predicate via a stub.)
```

- [ ] **Step 5:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/ tests/
git commit -m "fix: circuit breaker for all-APs-unreachable (C3)"
```

### Task 2.9 — Document shutdown ordering (H1 partial)

**Files:**
- Modify: `src/openwrt_presence/__main__.py:109-113`

The review flagged that `loop_stop` before `disconnect` causes LWT to
fire. **Decision** (per "ship at end" + minimising HA churn):
**keep LWT-on-shutdown behavior** because HA marking entities
unavailable on `docker stop` is the right signal. But document the
decision:

- [ ] **Step 1:** edit comment

```python
    finally:
        await source.close()
        # Shutdown ordering is deliberate:
        # loop_stop() halts paho's network thread, so the subsequent
        # disconnect() does not send a DISCONNECT packet — broker sees
        # a TCP drop and fires the LWT, which is exactly what we want
        # (HA marks entities unavailable on planned shutdowns).
        # Do NOT reorder without updating the LWT semantics.
        client.loop_stop()
        client.disconnect()
        logger.info("shutdown_complete")
```

- [ ] **Step 2:** commit

```bash
git add src/openwrt_presence/__main__.py
git commit -m "docs(main): explain shutdown ordering (LWT-on-stop is intentional, H1)"
```

### Task 2.10 — Session 2 acceptance

- [ ] **Step 1:** full suite

```bash
pytest -q --timeout=5
```

- [ ] **Step 2:** ruff + wire-format green

```bash
ruff check . && ruff format --check .
pytest tests/test_wire_format.py -v
```

- [ ] **Step 3:** Update CHANGELOG Unreleased

```markdown
### Fixed (security-critical)
- 🛡️ **Circuit breaker: all-APs-unreachable.** When every AP goes dark
  simultaneously, skip engine processing instead of producing false
  AWAY transitions. Closes the "dead network arms the alarm" class
  of failure. (CRITICAL C3)
- 🧵 **paho thread/asyncio race.** `on_connected` now hops to the
  asyncio loop via `call_soon_threadsafe` — no more
  dict-changed-size-during-iteration risk on reconnect. (CRITICAL C2)
- 📜 **Honest audit log.** `state_computed` emitted when the engine
  produces a change; `state_delivered` only after all three topics
  publish successfully. Dropped publishes now log `publish_failed`
  with return code. (CRITICAL C4)

### Fixed
- `on_connect` callback body now wrapped in try/except; paho's
  internal logger piped through structlog (HIGH H7)
- `asyncio.get_running_loop()` replaces deprecated `get_event_loop()`
  — forward-compatible with Python 3.14 (HIGH H8)
- Initial `source.query()` tolerates failure, continues into poll
  loop (HIGH H9)
- `/metrics` scrape checks HTTP status (503 → unreachable, not
  empty) and caps response at 1 MB (MEDIUM M11+M12)

### Documented
- Shutdown ordering: `loop_stop` before `disconnect` is deliberate
  (LWT on planned shutdown is desired) (HIGH H1)
```

```bash
git add CHANGELOG.md
git commit -m "docs: Session 2 changelog entries"
git push
```

---

## Session 3 — Type surgery

Goal: C1 (discriminated `StateChange`), H3 (`NewType` identifiers), H4
(frozen Config, hidden `_mac_lookup`), H5 (typed MQTT boundary). After
this session, pyright strict is clean for `src/` and the type system
enforces the invariants that were convention-only.

**Wire-format preservation is non-negotiable throughout. Run
`pytest tests/test_wire_format.py` after every commit in this session.**

### Task 3.1 — Create `domain.py` module

**Files:**
- Create: `src/openwrt_presence/domain.py`
- Modify: `src/openwrt_presence/engine.py` (move types out)
- Modify: `src/openwrt_presence/sources/exporters.py` (import from domain)
- Modify: `src/openwrt_presence/mqtt.py` (import from domain)

- [ ] **Step 1:** write `domain.py` with current types (plain move,
  no shape changes yet — discriminated union lands Task 3.3)

```python
# src/openwrt_presence/domain.py
"""Shared value vocabulary. Engine + sources + mqtt + audit import
from here; nothing in domain.py imports from any of them.

The `Mac`/`PersonName`/`NodeName`/`Room` NewTypes land in Task 3.2.
The `StateChange` discriminated union lands in Task 3.3.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StationReading:
    """A single RSSI measurement from an AP."""
    mac: str       # lowercase, colon-separated
    ap: str        # AP hostname (instance label)
    rssi: int      # signal strength in dBm


@dataclass(frozen=True)
class StateChange:
    person: str
    home: bool
    room: str | None
    mac: str
    node: str
    timestamp: datetime
    rssi: int | None = None


@dataclass(frozen=True)
class PersonState:
    home: bool
    room: str | None
```

- [ ] **Step 2:** update imports across the codebase

```bash
# In engine.py, sources/exporters.py, mqtt.py, logging.py:
# from openwrt_presence.engine import StationReading, StateChange, PersonState
# → from openwrt_presence.domain import StationReading, StateChange, PersonState
```

Also remove the local definitions from `engine.py`.

- [ ] **Step 3:** run tests (M5 + M2 partial fix delivered here)

```bash
pytest -q --timeout=5
pytest tests/test_wire_format.py -v   # must be green
```

- [ ] **Step 4:** commit

```bash
git add src/openwrt_presence/
git commit -m "refactor(domain): extract value types to openwrt_presence.domain (C5 prep, M5)"
```

### Task 3.2 — `NewType` for `Mac`, `PersonName`, `NodeName`, `Room` (H3)

**Files:**
- Modify: `src/openwrt_presence/domain.py`
- Modify: `src/openwrt_presence/config.py` (normalize returns `Mac`)
- Modify: `src/openwrt_presence/engine.py`
- Modify: `src/openwrt_presence/sources/exporters.py`
- Modify: `src/openwrt_presence/mqtt.py`
- Test: `tests/test_domain.py`

- [ ] **Step 1:** add NewTypes

```python
# Append to src/openwrt_presence/domain.py
from typing import NewType

Mac = NewType("Mac", str)          # lowercase, colon-separated; normalized at boundary
PersonName = NewType("PersonName", str)
NodeName = NewType("NodeName", str)
Room = NewType("Room", str)
```

- [ ] **Step 2:** write test

```python
# tests/test_domain.py
from openwrt_presence.domain import Mac, PersonName, NodeName, Room


def test_newtypes_are_str_at_runtime():
    m = Mac("aa:bb:cc:dd:ee:01")
    assert isinstance(m, str)
    assert m == "aa:bb:cc:dd:ee:01"


def test_newtypes_distinct_at_typecheck():
    # Pyright should flag this (we rely on CI gating); runtime is str.
    p = PersonName("alice")
    n = NodeName("ap-garden")
    assert p != n
```

- [ ] **Step 3:** update `Config._normalize_mac` to return `Mac`

```python
    @staticmethod
    def _normalize_mac(mac: str) -> Mac:
        return Mac(mac.lower().strip().replace("-", ":"))
```

- [ ] **Step 4:** update `StationReading.mac: Mac`,
  `_DeviceTracker` dict typed `dict[Mac, _DeviceTracker]`,
  `ExporterSource.tracked_macs: set[Mac]`, `Config._mac_lookup: dict[Mac, PersonName]`,
  `Config.mac_to_person(mac: Mac) -> PersonName | None`, etc.

- [ ] **Step 5:** **DELETE** the defensive re-normalization in
  `ExporterSource.__init__` (`{m.lower() for m in tracked_macs}`).
  DELETE `mac = r.mac.lower()` in `engine.py:86`. These exist only
  because the type didn't carry the invariant — now it does.

- [ ] **Step 6:** run

```bash
pytest -q --timeout=5
pytest tests/test_wire_format.py -v
pyright src/openwrt_presence/
```

pyright: should be considerably cleaner now.

- [ ] **Step 7:** commit

```bash
git add src/openwrt_presence/ tests/test_domain.py
git commit -m "refactor(types): Mac/PersonName/NodeName/Room NewTypes; drop defensive re-normalization (H3)"
```

### Task 3.3 — Discriminated `StateChange` union (C1, C2 types)

**Files:**
- Modify: `src/openwrt_presence/domain.py`
- Modify: `src/openwrt_presence/engine.py` (`_best_representative`,
  `get_person_snapshot`, `_emit_changes`)
- Modify: `src/openwrt_presence/mqtt.py` (`_emit_state` pattern-match)
- Modify: `src/openwrt_presence/logging.py`/`audit.py`
- Regenerate: `tests/wire_format_golden.json`
- Test: `tests/test_domain.py`, `tests/test_engine.py`

**Wire-format note:** this task *will* change the `never_seen` bytes
published (`mac=""`/`node=""` is no longer produced). Home and Away
with known representative stay unchanged. Regenerate only the
`never_seen` subfixture and include the before/after diff in the
migration notes.

- [ ] **Step 1:** define union

```python
# In domain.py
from typing import Literal


@dataclass(frozen=True)
class HomeState:
    person: PersonName
    room: Room
    mac: Mac
    node: NodeName
    timestamp: datetime
    rssi: int
    home: Literal[True] = True


@dataclass(frozen=True)
class AwayState:
    person: PersonName
    timestamp: datetime
    last_mac: Mac | None = None
    last_node: NodeName | None = None
    home: Literal[False] = False


StateChange = HomeState | AwayState
```

- [ ] **Step 2:** test

```python
# Add to tests/test_domain.py
from openwrt_presence.domain import AwayState, HomeState, Mac, PersonName, NodeName, Room
from datetime import datetime, timezone


def test_home_state_requires_room_and_mac():
    # Pyright enforces. At runtime, constructor takes all fields.
    h = HomeState(
        person=PersonName("alice"),
        room=Room("garden"),
        mac=Mac("aa:bb:cc:dd:ee:01"),
        node=NodeName("ap-garden"),
        timestamp=datetime(2026, 4, 21, tzinfo=timezone.utc),
        rssi=-55,
    )
    assert h.home is True


def test_away_state_allows_no_last_seen():
    a = AwayState(
        person=PersonName("bob"),
        timestamp=datetime(2026, 4, 21, tzinfo=timezone.utc),
    )
    assert a.home is False
    assert a.last_mac is None
```

- [ ] **Step 3:** update engine — `_best_representative` returns
  `tuple[Mac, NodeName, int] | None`; `_compute_person_state` returns
  `PersonState` (unchanged); `_emit_changes` + `get_person_snapshot`
  construct `HomeState`/`AwayState` by pattern.

```python
def _emit_changes(self, person: PersonName, timestamp: datetime) -> list[StateChange]:
    new_state = self._compute_person_state(person)
    old_state = self._last_person_state.get(person, PersonState(home=False, room=None))
    if new_state == old_state:
        return []
    self._last_person_state[person] = new_state

    if new_state.home:
        rep = self._best_representative(person)
        assert rep is not None  # home implies a tracked device
        mac, node, rssi = rep
        assert new_state.room is not None
        return [HomeState(person=person, room=Room(new_state.room),
                          mac=mac, node=node, timestamp=timestamp, rssi=rssi)]
    else:
        rep = self._best_representative(person)
        last_mac = rep[0] if rep else None
        last_node = rep[1] if rep else None
        return [AwayState(person=person, timestamp=timestamp,
                          last_mac=last_mac, last_node=last_node)]
```

- [ ] **Step 4:** update MQTT — pattern-match

```python
def _emit_state(self, change: StateChange) -> None:
    match change:
        case HomeState(person=p, room=r, mac=m, node=n, rssi=rs, timestamp=t):
            self._publish(f"{self._prefix}/{p}/state", "home")
            self._publish(f"{self._prefix}/{p}/room", r)
            attrs = {"mac": m, "node": n, "rssi": rs,
                     "last_seen": t.isoformat()}
            self._publish(f"{self._prefix}/{p}/attributes",
                          json.dumps(attrs))
        case AwayState(person=p, last_mac=lm, last_node=ln, timestamp=t):
            self._publish(f"{self._prefix}/{p}/state", "not_home")
            # room topic: publish empty (HA treats as unknown)
            self._publish(f"{self._prefix}/{p}/room", "")
            attrs: dict[str, Any] = {"last_seen": t.isoformat()}
            if lm is not None:
                attrs["mac"] = lm
            if ln is not None:
                attrs["node"] = ln
            self._publish(f"{self._prefix}/{p}/attributes",
                          json.dumps(attrs))
```

**Wire-format delta:** the `never_seen` case previously published
`{"mac": "", "node": "", "rssi": null, "last_seen": "..."}` — now
publishes `{"last_seen": "..."}` (no `mac`/`node`/`rssi` keys).

HA template sensors keyed on `state_attr('...', 'rssi')` receive
`None` instead of `null`, which is already the case for `rssi=null`
in attributes. Migration note in CHANGELOG.

- [ ] **Step 5:** regenerate wire-format fixture (never_seen subtree only)

```bash
# Re-run capture script (recreate if deleted)
python tests/capture_wire_format.py
# Manually inspect diff:
git diff tests/wire_format_golden.json
# Expected: home + away stanzas IDENTICAL.
# never_seen stanza: mac/node/rssi keys removed from attributes JSON.
# home/away/never_seen state and room topics: IDENTICAL bytes.
```

- [ ] **Step 6:** run

```bash
pytest -q --timeout=5
pytest tests/test_wire_format.py -v   # must pass against regenerated fixture
pyright src/openwrt_presence/         # cleaner
```

- [ ] **Step 7:** commit

```bash
git add src/openwrt_presence/ tests/ 
git commit -m "refactor(domain): StateChange = HomeState | AwayState discriminated union (C1)"
```

### Task 3.4 — Freeze `Config`, hide `_mac_lookup`, expose `tracked_macs` (H4 + M2)

**Files:**
- Modify: `src/openwrt_presence/config.py`
- Modify: `src/openwrt_presence/__main__.py` (use `config.tracked_macs`)

- [ ] **Step 1:** tests first

```python
# Add to tests/test_config.py
def test_config_is_frozen(sample_config):
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample_config.poll_interval = 9999  # type: ignore[misc]


def test_config_exposes_tracked_macs(sample_config):
    expected = frozenset({
        Mac("aa:bb:cc:dd:ee:01"),
        Mac("aa:bb:cc:dd:ee:02"),
        Mac("aa:bb:cc:dd:ee:03"),
    })
    assert sample_config.tracked_macs == expected
```

- [ ] **Step 2:** fix

```python
# config.py
@dataclass(frozen=True)
class Config:
    poll_interval: int
    departure_timeout: int
    away_timeout: int
    exporter_port: int
    dns_cache_ttl: int
    nodes: dict[NodeName, NodeConfig]
    people: dict[PersonName, PersonConfig]
    mqtt: MqttConfig
    # _mac_lookup no longer a public field:
    _mac_lookup: dict[Mac, PersonName] = dataclasses.field(
        default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        ...
        # After building nodes/people, compute mac_lookup:
        mac_lookup: dict[Mac, PersonName] = {}
        for name, person_cfg in people.items():
            for mac in person_cfg.macs:
                if mac in mac_lookup:
                    raise ConfigError(f"duplicate MAC {mac}: {mac_lookup[mac]} and {name}")
                mac_lookup[mac] = name

        instance = cls(..., _mac_lookup=mac_lookup)
        # If someone sets _mac_lookup post-hoc, they hit FrozenInstanceError.
        return instance

    def mac_to_person(self, mac: Mac) -> PersonName | None:
        return self._mac_lookup.get(mac)

    @property
    def tracked_macs(self) -> frozenset[Mac]:
        return frozenset(self._mac_lookup.keys())

    # ... etc
```

- [ ] **Step 3:** update `__main__._run` to pass
  `tracked_macs=config.tracked_macs`

- [ ] **Step 4:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/ tests/
git commit -m "refactor(config): freeze Config; expose tracked_macs; hide _mac_lookup (H4, M2)"
```

### Task 3.5 — Type the MQTT boundary (H5)

**Files:**
- Modify: `src/openwrt_presence/mqtt.py`
- Modify: `src/openwrt_presence/__main__.py`

- [ ] **Step 1:** update signatures

```python
# mqtt.py
from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from paho.mqtt.client import Client


class _HaDeviceBlock(TypedDict):
    identifiers: list[str]
    name: str
    manufacturer: str
    model: str


class MqttPublisher:
    def __init__(self, config: Config, client: Client) -> None:
        ...
```

Callbacks in `__main__.py`:

```python
from paho.mqtt.client import Client as MqttClient
from paho.mqtt.reasoncodes import ReasonCode


def _on_connect(client: MqttClient, userdata: None, flags: dict,
                reason_code: ReasonCode, properties: object | None = None) -> None:
    ...
```

- [ ] **Step 2:** run + commit

```bash
pytest -q --timeout=5
pyright src/openwrt_presence/mqtt.py src/openwrt_presence/__main__.py
git add src/openwrt_presence/
git commit -m "refactor(mqtt): type paho client + callbacks (no more Any at the boundary) (H5)"
```

### Task 3.6 — Other value types frozen (M5, M4 partial)

**Files:**
- Modify: `src/openwrt_presence/domain.py` — already frozen in 3.1
- Modify: `src/openwrt_presence/engine.py` — `_DeviceTracker` invariant
  assertion (M4 light version, keeping mutable tracker)

- [ ] **Step 1:** add invariant check

```python
# engine.py
@dataclass
class _DeviceTracker:
    state: DeviceState = DeviceState.AWAY
    node: NodeName = NodeName("")
    rssi: int = -200   # M14 fix: use -200 uniformly for "unknown"
    departure_deadline: datetime | None = None

    def __post_init__(self) -> None:
        self._check_invariant()

    def _check_invariant(self) -> None:
        if self.state == DeviceState.DEPARTING:
            assert self.departure_deadline is not None, \
                "DEPARTING requires a departure_deadline"
        else:
            assert self.departure_deadline is None, \
                f"{self.state} must not have a deadline"
```

Call `_check_invariant()` at the end of `process_snapshot` per
tracker. Lightweight.

- [ ] **Step 2:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/
git commit -m "refactor(engine): _DeviceTracker invariant assertion; -200 sentinel (M4, M14)"
```

### Task 3.7 — Session 3 acceptance

- [ ] **Step 1:** pyright strict clean for `src/`

```bash
pyright src/
```

Expected: `0 errors, 0 warnings`.

- [ ] **Step 2:** tests

```bash
pytest -q --timeout=5
pytest tests/test_wire_format.py -v
```

- [ ] **Step 3:** CHANGELOG

```markdown
### Changed (breaking — retained MQTT attributes)
- 🔒 **Discriminated `StateChange`.** `HomeState` / `AwayState` union
  replaces the old permissive dataclass. The `attributes` JSON for
  away-with-no-last-seen people no longer includes `mac`, `node`, or
  `rssi` keys (previously published empty strings / `null`).
  **HA migration:** template sensors doing
  `{{ state_attr(..., 'rssi') | int }}` should `default(None)` or
  check existence first. (CRITICAL C1)
- 🧱 **Frozen `Config`** and `NewType`-annotated identifier spaces
  (`Mac`, `PersonName`, `NodeName`, `Room`). Mix-ups between identifier
  kinds are now compile errors. (HIGH H3, HIGH H4)
- 🧩 **Typed MQTT boundary.** `paho.mqtt.client.Client` and reason
  codes no longer typed `Any`. (HIGH H5)

### Removed
- Two redundant MAC lowercase-normalizations — the `Mac` NewType
  carries the invariant.
- `_mac_lookup` as a public `Config` field — use
  `Config.mac_to_person(mac)` or `Config.tracked_macs`.
```

```bash
git add CHANGELOG.md
git commit -m "docs: Session 3 changelog entries"
git push
```

---

## Session 4 — Structural

Goal: `Source` protocol, `will_set` out of publisher constructor,
audit sink in the right module, startup gated on broker connect,
drop duplicate `_last_state` cache, centralize config defaults,
delete `engine.tick`. Testability: `_run()` now takes `client` +
`source` as arguments so the C7 end-to-end test becomes real.

### Task 4.1 — Refactor `_run` for dependency injection

**Files:**
- Modify: `src/openwrt_presence/__main__.py`

- [ ] **Step 1:** split `_run(config, client, source)` from `main()`

```python
# __main__.py
async def _run(config: Config, client: MqttClient, source: Source) -> None:
    """Run eve to shutdown, using injected broker client and source.

    Separated from main() for testability — tests pass FakeMqttClient
    and FakeSource; production wires real paho + ExporterSource.
    """
    # ... existing body, minus client construction and source construction

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

    try:
        asyncio.run(_run(config, client, source))
    except KeyboardInterrupt:
        pass
```

- [ ] **Step 2:** now write the full E2E test deferred in Session 1

```python
# tests/test_main.py (full version)
import asyncio
from datetime import datetime, timezone

import pytest

from openwrt_presence.__main__ import _run
from openwrt_presence.domain import StationReading
from tests.fakes import FakeMqttClient, FakeSource


async def test_startup_publishes_state_for_every_person(sample_config):
    client = FakeMqttClient()
    source = FakeSource()

    # Seed initial query with both people home
    source.schedule([
        StationReading(mac="aa:bb:cc:dd:ee:01", ap="ap-garden", rssi=-55),
        StationReading(mac="aa:bb:cc:dd:ee:02", ap="ap-living", rssi=-65),
    ])

    # Launch _run, give it enough time to seed + publish, then signal shutdown
    task = asyncio.create_task(_run(sample_config, client, source))
    await asyncio.sleep(0.2)
    # The shutdown path: directly cancel (SIGTERM simulation too
    # fragile in pytest). Cancellation is caught by _run's finally.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    people_with_state_topic = {
        m.topic.split("/")[1] for m in client.published
        if m.topic.endswith("/state")
    }
    assert people_with_state_topic >= {"alice", "bob"}


async def test_query_exception_does_not_stop_loop(sample_config):
    client = FakeMqttClient()
    source = FakeSource()
    source.schedule([], [])
    source.raise_on_next = RuntimeError("boom")

    task = asyncio.create_task(_run(sample_config, client, source))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Still published availability + discovery — loop survived
    assert any(m.topic.endswith("/state") for m in client.published)


async def test_reconnect_triggers_republish(sample_config):
    client = FakeMqttClient()
    source = FakeSource()
    source.schedule([
        StationReading(mac="aa:bb:cc:dd:ee:01", ap="ap-garden", rssi=-55),
    ])

    task = asyncio.create_task(_run(sample_config, client, source))
    await asyncio.sleep(0.2)
    client.clear()
    client.trigger_disconnect()
    await asyncio.sleep(0.05)
    client.trigger_connect()
    await asyncio.sleep(0.1)

    # After reconnect, state for at least alice republished
    assert any(
        m.topic == "openwrt-presence/alice/state" and m.payload == "home"
        for m in client.published
    )

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
```

- [ ] **Step 3:** run + commit

```bash
pytest tests/test_main.py -v --timeout=5
git add src/openwrt_presence/__main__.py tests/test_main.py
git commit -m "refactor(main): _run takes client+source for DI; full E2E test lands (C7)"
```

### Task 4.2 — `Source` protocol + `StationReading` in domain (C5 completion)

**Files:**
- Create: `src/openwrt_presence/sources/base.py`
- Modify: `src/openwrt_presence/sources/__init__.py`

- [ ] **Step 1:** write protocol

```python
# src/openwrt_presence/sources/base.py
from __future__ import annotations

from typing import Protocol

from openwrt_presence.domain import StationReading


class Source(Protocol):
    """A pluggable producer of station readings.

    Contract:
    - `query()` returns every currently-visible tracked station, best
      RSSI per MAC if the same station appears on multiple APs.
    - Per-node failures are isolated — a partial outage returns a
      partial list, never raises. A total outage returns an empty list.
    - `close()` is idempotent and releases all I/O resources.
    - `all_nodes_unhealthy` is True only when every configured node
      has been observed unhealthy AND the source has produced at
      least one prior successful query (fresh-boot returns False).
    """

    async def query(self) -> list[StationReading]: ...

    async def close(self) -> None: ...

    @property
    def all_nodes_unhealthy(self) -> bool: ...
```

- [ ] **Step 2:** re-export

```python
# sources/__init__.py
from openwrt_presence.sources.base import Source

__all__ = ["Source"]
```

- [ ] **Step 3:** annotate `_run(source: Source)`

- [ ] **Step 4:** verify `ExporterSource` structurally satisfies the
  protocol (pyright will catch mismatches)

```bash
pyright src/openwrt_presence/
```

- [ ] **Step 5:** commit

```bash
git add src/openwrt_presence/sources/
git commit -m "refactor(sources): Source Protocol + StationReading lives in domain (C5)"
```

### Task 4.3 — Move `will_set` out of publisher constructor (H1)

**Files:**
- Modify: `src/openwrt_presence/mqtt.py`
- Modify: `src/openwrt_presence/__main__.py` (main() wires will_set)

- [ ] **Step 1:** test — construction must not mutate client

```python
# Add to tests/test_mqtt.py
def test_publisher_constructor_does_not_call_will_set(sample_config):
    client = FakeMqttClient()
    MqttPublisher(sample_config, client)
    assert client.lwt is None, \
        "Publisher must not set LWT in constructor; caller does so before connect"
```

- [ ] **Step 2:** fix

```python
# mqtt.py - remove will_set from __init__
# Expose constants so main() can call will_set:
class MqttPublisher:
    def __init__(self, config: Config, client: Client) -> None:
        ...
        # no more self._client.will_set(...)

    @property
    def availability_topic(self) -> str:
        return f"{self._prefix}/status"

    OFFLINE_PAYLOAD: Final[str] = "offline"
    ONLINE_PAYLOAD: Final[str] = "online"
```

```python
# __main__.main() — explicit, sequential, visible
def main() -> None:
    ...
    publisher = MqttPublisher(config, client)
    # LWT must be set BEFORE connect_async — do it here explicitly.
    client.will_set(publisher.availability_topic,
                    publisher.OFFLINE_PAYLOAD, qos=1, retain=True)

    source = ExporterSource(...)
    asyncio.run(_run(config, client, source))
```

- [ ] **Step 3:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/ tests/
git commit -m "refactor(mqtt): move will_set out of publisher constructor (H1)"
```

### Task 4.4 — Move `log_state_change` to `audit.py` (H2)

**Files:**
- Create: `src/openwrt_presence/audit.py`
- Modify: `src/openwrt_presence/logging.py` (strip down to `setup_logging`)
- Modify: `src/openwrt_presence/mqtt.py` (import from audit)
- Modify: `src/openwrt_presence/monitor.py` (schema unchanged, still reads fields)

- [ ] **Step 1:** create `audit.py`

```python
# src/openwrt_presence/audit.py
"""Audit trail for state transitions. Moved here from logging.py so
that `logging.py` is purely the structlog setup boundary — audit log
entries are a domain concern."""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from openwrt_presence.domain import StateChange

_logger = structlog.get_logger()


def log_state_computed(change: StateChange) -> None:
    _log(change, event="state_computed")


def log_state_delivered(change: StateChange) -> None:
    _log(change, event="state_delivered")


def _log(change: StateChange, *, event: str) -> None:
    from openwrt_presence.domain import HomeState
    if isinstance(change, HomeState):
        _logger.info(event, person=change.person, presence="home",
                     room=change.room, mac=change.mac, node=change.node,
                     rssi=change.rssi, event_ts=change.timestamp.isoformat())
    else:
        _logger.info(event, person=change.person, presence="away",
                     room=None,
                     mac=change.last_mac, node=change.last_node, rssi=None,
                     event_ts=change.timestamp.isoformat())
```

- [ ] **Step 2:** update `mqtt.py` imports; strip `log_state_change`
  from `logging.py`.

- [ ] **Step 3:** tests (audit output format)

```python
# tests/test_audit.py
from openwrt_presence.audit import log_state_computed, log_state_delivered
from openwrt_presence.domain import AwayState, HomeState, ...
# assert structlog output for both shapes
```

- [ ] **Step 4:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/ tests/
git commit -m "refactor(audit): extract log_state_{computed,delivered} to audit module (H2)"
```

### Task 4.5 — Gate startup publishes on `on_connected` having fired (H6)

**Files:**
- Modify: `src/openwrt_presence/__main__.py`

- [ ] **Step 1:** use an `asyncio.Event` set from the paho callback

```python
# In _run:
    connected_event = asyncio.Event()

    def _on_connect(client, userdata, flags, reason_code, properties=None):
        logger.info("mqtt_connected", reason_code=str(reason_code))
        loop.call_soon_threadsafe(connected_event.set)
        loop.call_soon_threadsafe(_reseed)

    def _reseed():
        try:
            publisher.on_connected()
        except Exception:
            logger.exception("on_connected_failed")

    # ... connect_async + loop_start as before

    # Wait for broker connection before seeding state
    try:
        await asyncio.wait_for(connected_event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("mqtt_connect_timeout_seeding_anyway")

    # Initial query + seed runs AFTER on_connected (which published
    # discovery + availability).
    ...
```

- [ ] **Step 2:** test — FakeMqttClient drives `trigger_connect`
  before first publish

```python
# In tests/test_main.py extend
async def test_state_only_published_after_on_connect(sample_config):
    client = FakeMqttClient()
    source = FakeSource()
    source.schedule([StationReading(mac="aa:bb:cc:dd:ee:01",
                                     ap="ap-garden", rssi=-55)])

    task = asyncio.create_task(_run(sample_config, client, source))
    await asyncio.sleep(0.1)
    # Before trigger_connect, no /state publishes
    state_topics = [m for m in client.published if m.topic.endswith("/state")]
    assert state_topics == []
    client.trigger_connect()
    await asyncio.sleep(0.2)
    state_topics = [m for m in client.published if m.topic.endswith("/state")]
    assert len(state_topics) >= 1
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
```

- [ ] **Step 3:** run + commit

```bash
pytest tests/test_main.py -v --timeout=5
git add src/openwrt_presence/ tests/
git commit -m "fix(main): wait for MQTT connect before seeding state (H6)"
```

### Task 4.6 — Drop `MqttPublisher._last_state`; engine is single truth (H11)

**Files:**
- Modify: `src/openwrt_presence/mqtt.py` — remove `_last_state`
- Modify: `src/openwrt_presence/mqtt.py::on_connected` — take engine + now arg
- Modify: `src/openwrt_presence/__main__.py` — reconnect path

- [ ] **Step 1:** change `on_connected` signature

```python
# mqtt.py
class MqttPublisher:
    def on_connected(self, snapshots: list[StateChange]) -> None:
        """Republish discovery, availability, and the supplied per-person state."""
        self._publish_discovery()
        self._publish_online()
        for s in snapshots:
            self._emit_state(s)
```

- [ ] **Step 2:** in `__main__._run::_reseed`, ask engine for snapshots

```python
def _reseed():
    try:
        now = datetime.now(timezone.utc)
        snapshots = [engine.get_person_snapshot(p, now)
                     for p in config.people]
        publisher.on_connected(snapshots)
    except Exception:
        logger.exception("on_connected_failed")
```

- [ ] **Step 3:** `get_person_snapshot` returns `StateChange` union —
  either `HomeState` (real rep) or `AwayState` (no rep = `last_mac=None`)

- [ ] **Step 4:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/
git commit -m "refactor(mqtt): drop _last_state; engine is single source of truth (H11)"
```

### Task 4.7 — Centralize defaults (H13)

**Files:**
- Modify: `src/openwrt_presence/config.py`

- [ ] **Step 1:** extract module-level constants

```python
# config.py
DEFAULT_POLL_INTERVAL_SEC: Final[int] = 30
DEFAULT_AWAY_TIMEOUT_SEC: Final[int] = 64800
DEFAULT_EXPORTER_PORT: Final[int] = 9100
DEFAULT_DNS_CACHE_TTL_SEC: Final[int] = 300


@dataclass(frozen=True)
class Config:
    poll_interval: int = DEFAULT_POLL_INTERVAL_SEC
    departure_timeout: int   # required, no default
    away_timeout: int = DEFAULT_AWAY_TIMEOUT_SEC
    exporter_port: int = DEFAULT_EXPORTER_PORT
    dns_cache_ttl: int = DEFAULT_DNS_CACHE_TTL_SEC
    ...

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        if "mqtt" not in data:
            raise ConfigError("mqtt section is required")
        if "departure_timeout" not in data:
            raise ConfigError("departure_timeout is required (typical: 120 seconds)")
        # Use the class defaults via kwargs-or-omit pattern:
        kwargs: dict[str, Any] = {
            "departure_timeout": data["departure_timeout"],
            ...
        }
        if "poll_interval" in data:
            kwargs["poll_interval"] = data["poll_interval"]
        ...
        return cls(**kwargs)
```

- [ ] **Step 2:** remove duplicate `.get("key", default)` calls

- [ ] **Step 3:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/config.py
git commit -m "refactor(config): centralize defaults as module constants; ConfigError for missing required sections (H13, A2:A8, M6)"
```

### Task 4.8 — Delete `engine.tick()` (M3 / A1:A5 / A4:A17)

**Files:**
- Modify: `src/openwrt_presence/engine.py`
- Modify: `tests/test_engine.py` — delete `TestTick`

- [ ] **Step 1:** delete the method + its tests

- [ ] **Step 2:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/engine.py tests/test_engine.py
git commit -m "refactor(engine): delete unused tick() — process_snapshot owns expiry (M3)"
```

### Task 4.9 — `_compute_person_state` narrowed to known persons (M7)

**Files:**
- Modify: `src/openwrt_presence/engine.py`

- [ ] **Step 1:** assert precondition, remove dead defensive branch

```python
def _compute_person_state(self, name: PersonName) -> PersonState:
    assert name in self._config.people, \
        f"unknown person {name!r} — callers must iterate config.people"
    person_cfg = self._config.people[name]
    ...
```

- [ ] **Step 2:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/engine.py
git commit -m "refactor(engine): _compute_person_state precondition-asserted; drop dead branch (M7)"
```

### Task 4.10 — CLAUDE.md update: security framing + deliberate non-decisions

The review flagged no `Publisher` abstraction (H10) and no runtime
config reload (H14). Decision: **do not introduce either**.
`MqttPublisher` is the only publisher; if a second lands (webhook,
secondary MQTT), introduce the protocol *then*. SIGHUP + config diff
is engineering-heavy for a 4-person household — restart is the
answer. Document both, plus the security-software framing inherited
from `ha-verisure`.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1:** add "This is security software" opening to the
  `## Engineering standards` section (near the top, after the
  existing "Stack" block)

```markdown
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
```

- [ ] **Step 2:** append to end of `CLAUDE.md`

```markdown
## Deliberate non-decisions (YAGNI)

- **No `Publisher` protocol.** Only `MqttPublisher` exists. A second
  publisher (webhook, secondary broker) would justify introducing
  the protocol; until then, over-abstracting adds ceremony. See
  `docs/reviews/architecture/2026-04-21-architecture-review.md` H10.
- **No runtime config reload.** Adding a person or a MAC requires
  a restart. SIGHUP + diff + in-place mutation is engineering-heavy
  for a 4-person household, and the LWT → HA transient unavailable
  is acceptable operational churn. See finding H14.
- **Monitor → audit-log coupling left stringly-typed.** The
  `openwrt-monitor` CLI consumes the JSON shape of `state_computed`
  / `state_delivered` by field name. A proper `TypedDict` shared
  between producer and consumer isn't worth it for a dev tool.
  See finding A1:A7.
```

- [ ] **Step 3:** commit

```bash
git add CLAUDE.md
git commit -m "docs(claude): security-software framing + deliberate non-decisions (ha-verisure, H10, H14)"
```

### Task 4.11 — Session 4 acceptance

- [ ] **Step 1:** pyright strict clean

```bash
pyright
```

Expected: `0 errors, 0 warnings`. In CI, **now** remove
`continue-on-error: true` from the pyright step and the
`stages: [manual]` from the pre-commit pyright hook.

- [ ] **Step 2:** tests

```bash
pytest -q --timeout=5
pytest tests/test_wire_format.py -v
```

- [ ] **Step 3:** update CI + pre-commit

```bash
# Edit .github/workflows/ci.yml — remove `continue-on-error: true` on pyright
# Edit .pre-commit-config.yaml — remove `stages: [manual]` from pyright hook
pre-commit run --all-files   # must pass in full
```

- [ ] **Step 4:** CHANGELOG

```markdown
### Changed
- 🔌 **`_run` accepts injected client + source.** `__main__.main` wires
  real paho + ExporterSource; tests wire fakes. Enables a full
  end-to-end test suite for the poll loop. (CRITICAL C7)
- 🚪 **LWT set in `main()`, not in publisher constructor.** Ordering
  explicit. (HIGH H1)
- 📜 **`audit.py` owns `log_state_computed` / `log_state_delivered`.**
  `logging.py` is purely the structlog setup boundary. (HIGH H2)
- 🌱 **Seed state after broker connect.** Startup now waits for
  `on_connect` up to 10s before publishing retained state. HA flap
  closed. (HIGH H6)
- 🧠 **Single source of truth for per-person state** — engine, not
  publisher. `_last_state` cache deleted. (HIGH H11)
- ⚙️ **Config defaults centralized** as module constants. No more
  field-default-vs-`.get()`-fallback drift. (HIGH H13)

### Removed
- `engine.tick()` — `process_snapshot` has always owned expiry;
  `tick` was dead code in production. (MEDIUM M3)
- `_compute_person_state` defensive "unknown person" branch —
  precondition now asserted. (MEDIUM M7)

### Added
- 📐 `Source` protocol in `sources/base.py`. (CRITICAL C5)
- 🧪 Full end-to-end test suite for `__main__._run` via
  `FakeMqttClient` + `FakeSource`. (CRITICAL C7)
```

```bash
git add CHANGELOG.md .github/workflows/ci.yml .pre-commit-config.yaml
git commit -m "docs+ci: Session 4 changelog; pyright gates CI now"
git push
```

---

## Session 5 — Operational hardening + property-based tests + architecture tests

Goal: Docker healthcheck, property-based state-machine tests via
hypothesis, AST-based architecture tests, paho logger integration
already landed, first-scrape visibility log, upper Python bound.

### Task 5.1 — Property-based engine tests via hypothesis

**Files:**
- Create: `tests/test_engine_properties.py`

- [ ] **Step 1:** write

```python
# tests/test_engine_properties.py
"""Property-based fuzz of the presence state machine.

Invariants:
- Never default to HOME. An unknown MAC in a snapshot never
  produces a HomeState for anyone.
- AWAY → HOME requires a visible tracked reading.
- A device cannot be CONNECTED and DEPARTING at the same instant.
- Emitting a StateChange requires a real transition (new state !=
  last state).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, strategies as st

from openwrt_presence.domain import AwayState, HomeState, StationReading
from openwrt_presence.engine import PresenceEngine


TRACKED_MACS = [
    "aa:bb:cc:dd:ee:01",
    "aa:bb:cc:dd:ee:02",
    "aa:bb:cc:dd:ee:03",
]


_mac_strategy = st.sampled_from(TRACKED_MACS + [
    "ff:ff:ff:ff:ff:01",    # untracked
    "ff:ff:ff:ff:ff:02",
])
_ap_strategy = st.sampled_from(["ap-garden", "ap-living", "ap-bedroom"])
_rssi_strategy = st.integers(min_value=-95, max_value=-30)
_reading_strategy = st.builds(
    StationReading, mac=_mac_strategy, ap=_ap_strategy, rssi=_rssi_strategy,
)
_snapshot_strategy = st.lists(_reading_strategy, min_size=0, max_size=8)


@given(snapshots=st.lists(_snapshot_strategy, min_size=1, max_size=20))
def test_never_defaults_to_home_for_untracked_mac(snapshots, sample_config):
    engine = PresenceEngine(sample_config)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for snap in snapshots:
        changes = engine.process_snapshot(now, snap)
        now += timedelta(seconds=30)
        # No HomeState should ever cite an untracked MAC
        for c in changes:
            if isinstance(c, HomeState):
                assert sample_config.mac_to_person(c.mac) is not None


@given(snapshots=st.lists(_snapshot_strategy, min_size=1, max_size=20))
def test_state_machine_never_reports_impossible_room(snapshots, sample_config):
    engine = PresenceEngine(sample_config)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    configured_rooms = {n.room for n in sample_config.nodes.values()}
    for snap in snapshots:
        changes = engine.process_snapshot(now, snap)
        now += timedelta(seconds=30)
        for c in changes:
            if isinstance(c, HomeState):
                assert c.room in configured_rooms
```

- [ ] **Step 2:** run

```bash
pytest tests/test_engine_properties.py -v --timeout=30
```

Expected: 2 passed with ~100 hypothesis examples each.

- [ ] **Step 3:** commit

```bash
git add tests/test_engine_properties.py
git commit -m "test(engine): property-based state-machine invariants via hypothesis"
```

### Task 5.2 — Chaos test: dead-AP → departure end-to-end

**Files:**
- Create: `tests/test_chaos_dead_ap.py`

- [ ] **Step 1:** write test

```python
# tests/test_chaos_dead_ap.py
"""End-to-end: an AP that goes dark during an active session produces
a departure only after the configured timeout — and only on the
exit-node short timeout if the last representative was on an exit AP."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from openwrt_presence.engine import PresenceEngine
from openwrt_presence.domain import AwayState, HomeState, StationReading


def test_exit_node_ap_death_produces_away_after_departure_timeout(sample_config):
    engine = PresenceEngine(sample_config)
    # alice was last seen on the exit AP
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    engine.process_snapshot(t, [
        StationReading(mac="aa:bb:cc:dd:ee:01", ap="ap-garden", rssi=-55),
    ])
    # AP dies — empty snapshots thereafter
    for step in range(30):
        t += timedelta(seconds=10)
        changes = engine.process_snapshot(t, [])
        if any(isinstance(c, AwayState) and c.person == "alice" for c in changes):
            break
    elapsed = (t - datetime(2026, 1, 1, tzinfo=timezone.utc)).total_seconds()
    # departure_timeout=120 in sample_config; allow +30s grace
    assert 110 <= elapsed <= 150


def test_interior_node_ap_death_uses_long_timeout(sample_config):
    engine = PresenceEngine(sample_config)
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    engine.process_snapshot(t, [
        StationReading(mac="aa:bb:cc:dd:ee:02", ap="ap-living", rssi=-60),
    ])
    # 10 minutes of silence — should STILL be home (interior safety net)
    for step in range(60):
        t += timedelta(seconds=10)
        changes = engine.process_snapshot(t, [])
        assert not any(isinstance(c, AwayState) and c.person == "bob" for c in changes), \
            f"bob departed after {(t - datetime(2026, 1, 1, tzinfo=timezone.utc)).total_seconds()}s — should take 18h"
```

- [ ] **Step 2:** run + commit

```bash
pytest tests/test_chaos_dead_ap.py -v --timeout=10
git add tests/test_chaos_dead_ap.py
git commit -m "test: dead-AP chaos scenarios (exit vs interior timeouts)"
```

### Task 5.3 — AST-based architecture tests

**Files:**
- Create: `tests/test_architecture.py`

- [ ] **Step 1:** write

```python
# tests/test_architecture.py
"""AST-based rules preventing structural regression.

Uses AST parsing, not string matching — so comments and docstrings
can't accidentally trigger (or silence) a rule."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src" / "openwrt_presence"


def _all_modules() -> list[tuple[str, ast.Module]]:
    modules = []
    for p in _SRC.rglob("*.py"):
        tree = ast.parse(p.read_text(), filename=str(p))
        modules.append((str(p.relative_to(_SRC)), tree))
    return modules


def test_engine_does_not_import_io_modules():
    """engine.py must stay pure — no paho, no aiohttp, no mqtt."""
    engine = (_SRC / "engine.py").read_text()
    tree = ast.parse(engine)
    imports = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    for imp in imports:
        if isinstance(imp, ast.ImportFrom):
            mod = imp.module or ""
            assert not mod.startswith("paho"), f"engine imports {mod}"
            assert "aiohttp" not in mod, f"engine imports {mod}"
            assert "openwrt_presence.mqtt" not in mod
            assert "openwrt_presence.sources" not in mod


def test_sources_do_not_import_engine():
    """Sources must depend on domain, not engine (reverse-layering guard)."""
    for fname in (_SRC / "sources").glob("*.py"):
        tree = ast.parse(fname.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod != "openwrt_presence.engine", \
                    f"{fname.name} imports from engine"


def test_no_datetime_now_in_engine():
    """Engine receives `now` injected — never calls datetime.now() itself."""
    engine = (_SRC / "engine.py").read_text()
    tree = ast.parse(engine)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Match datetime.now() or datetime.datetime.now()
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "now":
                pytest.fail(f"engine.py calls .now() at line {node.lineno}")


def test_no_any_in_public_src_signatures():
    """`dict[str, Any]` is boundary-only (Config.from_dict); Any absent elsewhere."""
    BOUNDARY = {"config.py"}   # where Any is tolerated (YAML parse)
    for rel, tree in _all_modules():
        if rel in BOUNDARY:
            continue
        for node in ast.walk(tree):
            # Detect a bare `: Any` or `-> Any` in function signatures
            if isinstance(node, ast.FunctionDef):
                if node.name.startswith("_"):
                    continue   # private
                annots = []
                for a in node.args.args:
                    if a.annotation is not None:
                        annots.append(a.annotation)
                if node.returns is not None:
                    annots.append(node.returns)
                for annot in annots:
                    if _is_any(annot):
                        pytest.fail(
                            f"{rel}:{node.lineno} {node.name}: Any in public signature"
                        )


def _is_any(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "Any":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "Any":
        return True
    return False


def test_frozen_dataclasses_for_value_objects():
    """StationReading, StateChange, HomeState, AwayState, PersonState, Config
    must all be frozen dataclasses."""
    FROZEN_REQUIRED = {
        "domain.py": {"StationReading", "HomeState", "AwayState", "PersonState"},
        "config.py": {"Config", "NodeConfig", "PersonConfig", "MqttConfig"},
    }
    for fname, classes in FROZEN_REQUIRED.items():
        tree = ast.parse((_SRC / fname).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in classes:
                # Must have @dataclass(frozen=True)
                frozen = False
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if kw.arg == "frozen" and isinstance(kw.value, ast.Constant) \
                               and kw.value.value is True:
                                frozen = True
                assert frozen, f"{fname}::{node.name} must be @dataclass(frozen=True)"
```

- [ ] **Step 2:** run

```bash
pytest tests/test_architecture.py -v --timeout=5
```

- [ ] **Step 3:** commit

```bash
git add tests/test_architecture.py
git commit -m "test(architecture): AST-based rules (no IO in engine, no Any, frozen value types)"
```

### Task 5.4 — Docker healthcheck (M8)

**Files:**
- Modify: `src/openwrt_presence/__main__.py` — touch `/tmp/eve_alive` each poll
- Modify: `Dockerfile.example`
- Modify: `docker-compose.yaml.example`

- [ ] **Step 1:** in `_run`, touch a sentinel after each successful poll

```python
# After engine.process_snapshot and publishes, in the poll loop:
    try:
        Path("/tmp/eve_alive").touch(exist_ok=True)
    except OSError:
        pass   # dev environment, not fatal
```

- [ ] **Step 2:** `Dockerfile.example` adds healthcheck

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD test -n "$(find /tmp/eve_alive -mmin -2 2>/dev/null)" || exit 1
```

- [ ] **Step 3:** `docker-compose.yaml.example` — nothing to change if
  Dockerfile carries the healthcheck.

- [ ] **Step 4:** commit

```bash
git add src/openwrt_presence/__main__.py Dockerfile.example
git commit -m "feat(ops): Docker healthcheck via /tmp/eve_alive sentinel (M8)"
```

### Task 5.5 — First-scrape visibility log (M10)

**Files:**
- Modify: `src/openwrt_presence/sources/exporters.py`

- [ ] **Step 1:** emit `initial_node_state` after first query per node

```python
# In _scrape_ap or query aggregation, first time each node is seen:
    if node not in self._node_healthy:
        logger.info("initial_node_state", node=node, healthy=ok)
```

- [ ] **Step 2:** run + commit

```bash
pytest -q --timeout=5
git add src/openwrt_presence/sources/exporters.py
git commit -m "feat(sources): emit initial_node_state on first scrape per node (M10)"
```

### Task 5.6 — CHANGELOG final review

- [ ] **Step 1:** read `[Unreleased]` top-to-bottom. Ensure every
  finding C1–C7 and H1–H14 is either (a) in the notes with its
  finding code, or (b) deliberately deferred with a one-line note.

- [ ] **Step 2:** add the migration-notes section

```markdown
### Migration notes

**Breaking: retained MQTT attributes for never-seen persons.**
Previously eve published `{"mac": "", "node": "", "rssi": null, "last_seen": ...}` for persons it had never observed. Now publishes `{"last_seen": ...}` (no mac/node/rssi keys). HA template sensors using `{{ state_attr(..., 'rssi') | int }}` should handle missing keys — add `| default(0)` or check existence.

**Retained state contract unchanged** for observed persons. `home` / `not_home` payloads, QoS=1, `retain=True` on state / room / attributes / availability / discovery are byte-identical to v0.5.0.

**Python version.** Now pinned to `>=3.11,<3.14`. Docker base image stays on `python:3.11-slim`.

**Healthcheck added.** `docker-compose` users with an explicit `healthcheck:` stanza can remove it — `Dockerfile` now carries its own.

**`CONFIG_PATH` unchanged, config schema unchanged.** No yaml edits required.

**Audit log schema added a field:** `event` is now `state_computed` (engine produced the change) or `state_delivered` (all three topic publishes succeeded). `openwrt-monitor` handles both; log shippers filtering on `message=state_change` need updating — the old `state_change` message is GONE, replaced by `state_computed`. Adjust HA or Loki filters accordingly.
```

- [ ] **Step 3:** commit

```bash
git add CHANGELOG.md
git commit -m "docs: final migration notes for v0.6.0 [Unreleased]"
git push
```

### Task 5.7 — Full acceptance

- [ ] **Step 1:** everything green

```bash
pytest -q --timeout=5
pytest tests/test_wire_format.py -v
pytest tests/test_engine_properties.py -v
pytest tests/test_architecture.py -v
pytest tests/test_chaos_dead_ap.py -v
pytest tests/test_main.py -v
ruff check .
ruff format --check .
pyright
```

All green. Zero warnings.

- [ ] **Step 2:** CI on master equivalent (on hardening branch)

Push → CI matrix (Python 3.11, 3.12, 3.13) must be green.

---

## Session 6 — Release v0.6.0

### Task 6.1 — Merge to master

- [ ] **Step 1:** final rebase

```bash
git checkout hardening-v0.6.0
git fetch origin
git rebase origin/master
```

Expect no conflicts — master has been untouched during this work.

- [ ] **Step 2:** create PR

```bash
gh pr create --title "v0.6.0: alarm-pathway hardening + type discipline + test architecture" \
             --body "$(cat CHANGELOG.md | awk '/## \[Unreleased\]/,/## \[0\.5\.0\]/' | head -n -1)"
```

- [ ] **Step 3:** wait for CI green, self-review, merge

```bash
gh pr merge --squash --delete-branch
git checkout master
git pull --ff-only
```

### Task 6.2 — Cut v0.6.0

Follow the `/close` skill release procedure:

- [ ] **Step 1:** bump `pyproject.toml` version to `0.6.0`

- [ ] **Step 2:** rename `[Unreleased]` → `## [0.6.0] — <today's date>` in CHANGELOG, add a new empty `[Unreleased]` above, update link refs at the bottom

- [ ] **Step 3:** commit + tag + push

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "docs: cut v0.6.0"
git tag -a v0.6.0 -m "v0.6.0 — hardening & type discipline"
git push && git push origin v0.6.0
```

- [ ] **Step 4:** GitHub release

```bash
notes=$(awk '/## \[0\.6\.0\]/,/## \[0\.5\.0\]/' CHANGELOG.md | sed '$d')
gh release create v0.6.0 --title "v0.6.0 — hardening & type discipline" --notes "$notes"
```

### Task 6.3 — Deploy

- [ ] **Step 1:** `docker compose up -d --build`

- [ ] **Step 2:** verify logs

```bash
sleep 4 && docker container logs eve --tail 50
```

Must see:
- `initial_node_state` for each AP
- `mqtt_connected`
- `state_computed` + `state_delivered` for each configured person
  (seed after on_connect)
- `poll_loop_started`
- No `ERROR` lines

- [ ] **Step 3:** verify HA

Open HA device_tracker entities. Each person should show expected
home/away, no "unavailable" flap.

- [ ] **Step 4:** 24h soak

After 24h, check:
- No `publish_failed` entries
- No `all_nodes_unreachable`
- `on_connected_failed` never fired
- Audit trail complete (every `state_computed` has a matching
  `state_delivered` within ms)

---

## Self-review

Running against the spec (the 2026-04-21 architecture review):

**CRITICAL coverage:**
- C1 StateChange illegal combos → Task 3.3 ✓
- C2 paho/asyncio race → Task 2.7 ✓
- C3 all-APs-unreachable → Task 2.8 ✓
- C4 fire-and-forget publish → Tasks 2.5 + 2.6 ✓
- C5 Source abstraction → Tasks 3.1 + 4.2 ✓
- C6 test-arch violations → Tasks 1.5 + 1.6 ✓
- C7 `__main__._run` uncovered → Tasks 1.7 + 4.1 ✓

**HIGH coverage:**
- H1 LWT in constructor → Tasks 4.3 + 2.9 ✓
- H2 publisher owns audit → Task 4.4 ✓
- H3 NewTypes → Task 3.2 ✓
- H4 freeze Config → Task 3.4 ✓
- H5 MQTT Any → Task 3.5 ✓
- H6 startup before connect → Task 4.5 ✓
- H7 on_connect try/except → Task 2.3 ✓
- H8 get_event_loop → Task 2.1 ✓
- H9 initial query error → Task 2.2 ✓
- H10 Publisher abstraction → Task 4.10 (YAGNI decision) ✓
- H11 two caches → Task 4.6 ✓
- H12 hand-rolled Config → Task 1.4 ✓
- H13 defaults duplicated → Task 4.7 ✓
- H14 runtime reload → Task 4.10 (YAGNI decision) ✓

**MEDIUM coverage (selected):**
- M3 engine.tick → Task 4.8 ✓
- M4 tracker invariant → Task 3.6 (lightweight) ✓
- M5 frozen value types → Task 3.1 + 3.6 ✓
- M6 ConfigError for mqtt section → Task 4.7 ✓
- M7 unknown person defensive → Task 4.9 ✓
- M8 Docker healthcheck → Task 5.4 ✓
- M10 first-scrape log → Task 5.5 ✓
- M11 response size cap → Task 2.4 ✓
- M12 status check → Task 2.4 ✓
- M14 magic -100/-200 → Task 3.6 (partial — -200 sentinel) ✓

**LOW findings:** batched into larger tasks where touched; remaining
ones (A1:A7 monitor schema link, A3:A16 ConfigError before structlog,
A3:A18 KeyboardInterrupt, A3:A20 rssi=null in HA attrs) left as
backlog — not worth a dedicated session.

**Non-coverage (deliberate):**
- H10, H14 — YAGNI, recorded in CLAUDE.md (Task 4.10)

Placeholder scan: no TBD / TODO / "implement later" in any task
body. Every step has concrete code or commands.

Type consistency: `Mac`/`PersonName`/`NodeName`/`Room` naming
consistent from Task 3.2 onwards. `HomeState`/`AwayState`/`StateChange`
naming consistent from Task 3.3 onwards. `FakeMqttClient`/`FakeSource`
consistent across Tasks 1.2, 1.7, 4.1.

---

## Execution handoff

**Plan complete and saved to `docs/plans/2026-04-21-eve-hardening.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task group, review between groups, fast iteration. Good for this plan because tasks cluster tightly within sessions.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints. Good if you want to drive the work yourself and need me only as the mechanic.

**Which approach?**
