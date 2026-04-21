# Architecture review — robustness, failure modes, fail-secure

**Scope:** `/srv/eve/src/openwrt_presence/` (~975 LOC)
**Lens:** robustness, async/signal/shutdown correctness, alarm-pathway safety
**Date:** 2026-04-21

## Summary

The engine is clean and well-isolated (pure logic, injected `now`, domain
types at boundaries) and the paho v2 reconnect architecture is largely
correct. However, there are several real robustness issues in the
wiring layer that matter specifically because this feeds alarm
automations. The top three: (1) `publisher.on_connected()` runs on the
paho network thread and concurrently iterates/mutates `_last_state`
with the asyncio loop, a dictionary-size race that could crash the
process or corrupt the reconnect replay; (2) there is no detection or
fail-secure handling for "all APs unreachable," so a total scrape
outage will produce real `AWAY` transitions within `departure_timeout`
seconds and can arm the alarm; (3) `publish_state()` is fire-and-forget
with no delivery check, yet unconditionally writes the audit-log line,
so during broker downtime the log asserts "we told HA" while paho is
silently dropping overflowed messages. Several lower-severity issues
follow around shutdown ordering, deprecated `asyncio.get_event_loop()`,
error handling in paho callbacks, missing container healthcheck, and
the startup publish-before-connect ordering.

---

## CRITICAL

### A1. `publisher.on_connected()` races with asyncio loop on `_last_state`
**Concern:** robustness / concurrency
**Scope:** `src/openwrt_presence/mqtt.py:138-149`, `src/openwrt_presence/__main__.py:37-42,85-108`
**Problem:** `on_connected()` is invoked from paho's network thread
(`loop_start`) via `_on_connect` in `__main__.py:41`. It iterates
`self._last_state.values()` (line 148). The asyncio coroutine calls
`publisher.publish_state(change)` (`__main__.py:87,108`) which mutates
`self._last_state[change.person] = change` (`mqtt.py:93`). These two
accesses are unsynchronised. If a `publish_state` call inserts a brand
new person key while `on_connected` is iterating, CPython raises
`RuntimeError: dictionary changed size during iteration`. Even in the
non-resizing case, concurrent mutation of dict values is not
memory-safe across the GIL boundary for compound operations.
Additionally `_emit_state` itself (called from both threads) calls
`self._client.publish()` — paho's publish is thread-safe, but the
ordering of retained messages for the same topic is non-deterministic
between threads, so a stale cached state can overwrite a newer state
on the broker in a narrow window.
**Impact:** service crashes inside the paho thread (which may be
swallowed by paho — see A7), or the retained state for a person
momentarily regresses to an older value just after a reconnect. Because
HA consumes retained state, a flapping broker could briefly flip a
person from `home` back to `not_home` — which is the state that arms
the alarm.
**Recommendation:** do not touch shared state from the paho callback.
Hand off to the asyncio loop:

```python
# in __main__._run(), capture loop:
loop = asyncio.get_running_loop()

def _on_connect(client, userdata, flags, reason_code, properties=None):
    logger.info("mqtt_connected", reason_code=str(reason_code))
    loop.call_soon_threadsafe(publisher.on_connected)
```

Alternatively, guard `_last_state` with a `threading.Lock` inside
`MqttPublisher` and take a snapshot (`list(self._last_state.values())`)
under the lock before iterating. The `call_soon_threadsafe` approach
is preferable because it keeps MQTT publishing on one thread and is
less likely to rot.
**Severity:** CRITICAL

---

### A2. No fail-secure handling for "all APs unreachable"
**Concern:** fail-secure / alarm pathway
**Scope:** `src/openwrt_presence/sources/exporters.py:56-85`, `src/openwrt_presence/engine.py:73-135`, `src/openwrt_presence/__main__.py:99-108`
**Problem:** `ExporterSource.query()` isolates per-node failures (good)
but returns an empty reading list when every AP is unreachable. The
engine cannot distinguish "nobody is home" from "we are blind." All
currently-CONNECTED devices transition to DEPARTING, and after
`timeout_for_node(tracker.node)` expires they become AWAY. A device
last seen on an exit node has `departure_timeout` (120s default), so
within ~2 minutes of total scrape failure every person whose last
representative device was on an exit AP is published as `not_home`
with `retain=True`. CLAUDE.md explicitly flags this: "a dead AP can
generate false departures." The interior-node 18h safety net only
helps devices last seen on interior APs.
**Impact:** a network partition, a DNS outage, or the Docker host
losing connectivity to the AP VLAN produces *real, committed*
`not_home` transitions on MQTT. HA's
`alarm_control_panel.alarm_arm_away` automation arms the alarm while
the occupant is sitting in the garden.
**Recommendation:** add a circuit breaker at the source or main-loop
level: if **all** nodes are currently unhealthy
(`all(not v for v in self._node_healthy.values())` when it has any
entries), skip engine processing for this cycle. Emit a loud
`logger.error("all_nodes_unreachable", ...)` transition. Resume engine
processing once at least one node recovers. Consider additionally
freezing departure deadlines that were last updated on a now-unhealthy
node — i.e., if a device was CONNECTED on `ap-garden` and `ap-garden`
just transitioned unhealthy, extend or clear its departure deadline
until that AP recovers or the 18h interior safety-net fires.
**Severity:** CRITICAL

---

### A3. `publish_state()` is fire-and-forget but writes the audit-log line unconditionally
**Concern:** data integrity / alarm pathway
**Scope:** `src/openwrt_presence/mqtt.py:85-123`, `src/openwrt_presence/__main__.py:32`
**Problem:** `self._client.publish(...)` returns an `MQTTMessageInfo`
whose `rc` and `mid` are never inspected, and the code never awaits
publication confirmation. `log_state_change(change)` is called
immediately after the three `publish()` calls, asserting "we told HA."
The client is configured with `max_queued_messages_set(1000)` — when
the broker is down, paho accumulates up to 1000 messages in-process
before silently dropping the overflow. Given ~3 publishes per
transition (state, room, attributes) and the startup seed of 3×N
publishes at boot, plus normal transitions, the buffer can overflow
in minutes of broker downtime.
**Impact:** audit log claims a state change was delivered when it was
not. On a 1001st-message overflow, paho returns `MQTT_ERR_QUEUE_SIZE`
from `publish()` and drops the message — no log, no HA update, but a
`state_change` line in the audit trail. For a security service this
breaks the "log every state change" contract in a particularly
insidious way: the log is wrong, not merely silent.
**Recommendation:** check `info.rc` from each `publish()` call; if it
is not `MQTT_ERR_SUCCESS`, emit `logger.error("publish_failed", ...)`
with topic and return code. Either (a) still emit the audit line but
include the delivery outcome, or (b) separate "state computed" from
"state delivered" in the log schema so the audit trail stays honest.
Consider raising the queued-message cap or switching to a proper
persistent-session MQTT setup so messages survive service restarts,
not just broker hiccups.
**Severity:** CRITICAL

---

## HIGH

### A4. Shutdown ordering sends DISCONNECT after stopping the network loop
**Concern:** shutdown correctness / LWT semantics
**Scope:** `src/openwrt_presence/__main__.py:109-113`
**Problem:** the `finally` block calls, in order: `source.close()`,
`client.loop_stop()`, `client.disconnect()`. `loop_stop()` terminates
paho's network thread. Calling `disconnect()` after that point does
not send a DISCONNECT packet because no thread is processing the
socket; the broker observes a TCP close instead of a clean disconnect,
and the LWT fires. For this specific service that is arguably the
desired outcome (service gone → HA marks entities unavailable), but
the intent is invisible and one future refactor ("clean up shutdown,
do disconnect first") will silently break HA availability signalling.
The current code *appears* to try for a graceful shutdown but actually
relies on an ungraceful one.
**Impact:** either (a) a well-meaning refactor swaps the order and
suddenly HA never marks the service unavailable after `docker stop`,
or (b) the current behaviour looks right until someone reads CLAUDE.md
and wonders why LWT fires on SIGTERM.
**Recommendation:** decide and document. If LWT-on-shutdown is
intentional, leave the order and add a comment explaining *why*
(loop_stop before disconnect is deliberate: forces broker to see TCP
drop → LWT fires → HA marks unavailable). If clean shutdown is
desired, reorder: `client.disconnect()` first (sends DISCONNECT while
loop thread is alive), then `loop_stop()`, and publish an explicit
`status=offline` retained message before disconnecting so HA still
sees unavailability.
**Severity:** HIGH

---

### A5. Startup publishes state before the MQTT connection exists
**Concern:** ordering / data integrity
**Scope:** `src/openwrt_presence/__main__.py:52-88`
**Problem:** `client.connect_async()` + `loop_start()` are issued at
lines 52-53. Immediately afterwards (line 75 onwards) the code
performs `source.query()` and then `publisher.publish_state(snapshot)`
for every person. At this moment:
- TCP connection to the broker may or may not be established.
- `on_connect` has not yet fired, so `publish_discovery()` and
  `publish_online()` have not run.
- `_last_state` is being populated by `publish_state`, so the eventual
  `on_connected()` will iterate a dict that was just written.
HA MQTT Discovery expects a `discovery config` before it meaningfully
processes state topics — although HA is tolerant, state messages for
an unknown device_tracker may be discarded until discovery arrives.
Worse, if the broker is slow to come up, the paho queue absorbs the
initial seed; when `on_connected` finally fires it publishes discovery
AFTER the queued state messages have already been sent (they were
queued earlier). HA may momentarily treat the entity as unavailable
then snap to the newly arrived state.
**Impact:** occasional "unavailable → home → unavailable → home"
flaps in HA on service start, because availability is published from
`on_connected()` but state has already been queued. For the alarm
automation this is usually harmless (HA's alarm depends on stable
`not_home`) but it is avoidable churn.
**Recommendation:** publish discovery + availability BEFORE any state.
Either (a) gate startup publishes on `on_connected` having fired (wait
on an `asyncio.Event` set from `_on_connect` via
`loop.call_soon_threadsafe`), or (b) call `publisher.publish_discovery()`
and `publisher.publish_online()` explicitly at the top of `_run()`
before the first query. Option (a) is more correct because it also
survives an initial broker-down scenario (don't seed stale state into
a queue that may overflow).
**Severity:** HIGH

---

### A6. `_on_connect` callback has no error handling; paho silently swallows exceptions
**Concern:** error handling / reconnect correctness
**Scope:** `src/openwrt_presence/__main__.py:37-42`, `src/openwrt_presence/mqtt.py:138-149`
**Problem:** paho catches and discards exceptions raised from user
callbacks. If `publisher.on_connected()` raises — for example due to
A1 (dict RuntimeError), a JSON serialisation error in
`_publish_device_tracker_discovery` for an oddly-named person, or a
`TypeError` because `_last_state` contains an unexpected value — paho
logs the traceback to its internal logger (which is not wired into
structlog) and moves on. No structured log line lands in the service's
JSON output. The reconnect "re-seed" contract silently fails.
**Impact:** after a broker restart (e.g. Mosquitto major-version
upgrade dropping retained state, as the docstring specifically calls
out) discovery and cached state are never republished, and we do not
know because the error disappeared into paho. HA can end up with
stale or missing entities. This is a silent failure in the recovery
path we are explicitly relying on.
**Recommendation:** wrap `publisher.on_connected()` in a try/except
that logs the exception with structlog:

```python
def _on_connect(client, userdata, flags, reason_code, properties=None):
    logger.info("mqtt_connected", reason_code=str(reason_code))
    try:
        publisher.on_connected()
    except Exception:
        logger.exception("on_connected_failed")
```

Also attach paho's internal logger to the structlog pipeline
(`client.enable_logger(...)`) so paho's swallowed stack traces surface
as JSON.
**Severity:** HIGH

---

### A7. `asyncio.get_event_loop()` is deprecated and will break on Python 3.14+
**Concern:** forward compatibility / shutdown correctness
**Scope:** `src/openwrt_presence/__main__.py:65`
**Problem:** inside a coroutine running under `asyncio.run()`,
`asyncio.get_event_loop()` is deprecated since 3.12 (DeprecationWarning)
and will raise in 3.14. The correct API is
`asyncio.get_running_loop()`. Dependency on the running loop for
`add_signal_handler` must use the correct reference or signal delivery
may go to the wrong loop in future Python versions.
**Impact:** a Python upgrade (the project declares `requires-python
>=3.11`, with no upper bound) will crash startup with a RuntimeError,
and the Docker image's `python:3.11-slim` base will eventually be
bumped. Signal handlers simply won't be installed.
**Recommendation:** change to `loop = asyncio.get_running_loop()`.
**Severity:** HIGH

---

### A8. Initial `source.query()` at startup has no error handling
**Concern:** error handling / startup robustness
**Scope:** `src/openwrt_presence/__main__.py:75-78`
**Problem:** the main loop wraps `source.query()` in `try/except
Exception` (lines 99-103), but the startup query at line 76 does not.
`query()` isolates per-AP errors internally, but DNS failure in the
connector constructor, a `SessionConfigurationError`, or any unhandled
exception bubbles up and terminates the service before `loop_start()`
has had a chance to establish the MQTT connection. Docker's
`restart: unless-stopped` will then crash-loop the container.
**Impact:** startup brittleness inconsistent with the loop's own
tolerance. More importantly, if startup dies before the availability
topic is ever set to `online`, HA sees only the previously retained
`offline` (from the last LWT) and entities stay unavailable — which
is fail-secure but produces no alarm-actionable signal either.
**Recommendation:** wrap the initial query in the same try/except as
the main loop, log `logger.exception("initial_query_failed")`, and
proceed to the poll loop so recovery can happen on the next cycle.
**Severity:** HIGH

---

## MEDIUM

### A9. No Docker healthcheck; hung process is not restarted
**Concern:** operational / availability
**Scope:** `Dockerfile.example`, `docker-compose.yaml.example`
**Problem:** neither the Dockerfile nor the compose file defines a
`HEALTHCHECK`. `restart: unless-stopped` only restarts on process
exit. If the asyncio loop hangs (deadlock in paho callback, wedged
aiohttp session, etc.) the container stays "running" forever.
**Impact:** silent failure. All state retained-stale, no scrapes, no
MQTT publishes, but container looks healthy to Docker and to a
monitoring system that only checks container state.
**Recommendation:** add a healthcheck that asserts the poll loop is
alive. Cheap option: have `__main__` touch a sentinel file (`/tmp/alive`)
with `os.utime` each cycle, and a healthcheck of the form
`test -z "$(find /tmp/alive -mmin +1)"` or equivalent. Or publish a
periodic structured log `poll_cycle_completed` and let an external
monitor watch for it. This is especially important because a hang
also means the LWT never fires (TCP stays open), so HA cannot detect
the service is wedged either.
**Severity:** MEDIUM

---

### A10. `publish_state` called at startup before broker connects means log-before-delivery
**Concern:** data integrity / audit
**Scope:** `src/openwrt_presence/__main__.py:85-88`, `src/openwrt_presence/mqtt.py:85-95`
**Problem:** related to A3 and A5. At startup the seed emits one
`state_change` log line per person regardless of whether the publish
actually reached the broker (broker may be down or not yet connected).
Because the seed is the *first* log of each person's state, an
operator reading the audit trail sees "alice: home at 09:00:00" when
in reality HA may never have received it. If the subsequent poll loop
then doesn't produce a new transition (alice is still home), the
audit trail shows a `home` line but HA's retained state is still the
pre-restart `offline`/stale value. The "startup always emits one
state_change per person" comment in `__main__.py:80-84` frames this
as correct — but the correctness claim is about *log coverage*, not
*HA state*.
**Impact:** confusion during post-incident forensics. Harder to tell
from logs alone whether HA actually reflects what the engine computed.
**Recommendation:** solve with A3 (report the `rc`/delivery outcome
in the log) and A5 (don't seed before broker connects). Additionally
consider a distinct log event `state_seed` for the startup bulk
publish so forensic tools can tell a recovery from a real transition.
**Severity:** MEDIUM

---

### A11. `client.disconnect()` called after `loop_stop()` prevents clean offline publish
**Concern:** shutdown correctness (subtle)
**Scope:** `src/openwrt_presence/__main__.py:111-112`
**Problem:** related to A4. A common pattern to signal clean shutdown
to HA without relying on LWT is to publish `status=offline` explicitly
before disconnecting. The current shutdown skips this: after
`loop_stop()` no publish can occur. Combined with `disconnect()`
being effectively a no-op after `loop_stop()`, the only offline
signalling to HA is the LWT firing on TCP close. If the broker
happens to be stuck in a TCP-keepalive gap the LWT may not fire for
tens of seconds.
**Impact:** delayed "unavailable" in HA on planned shutdowns.
**Recommendation:** before shutting down paho, publish
`self._availability_topic` with payload `offline` and `retain=True`
and wait briefly (`info.wait_for_publish(timeout=2)`). Then
`loop_stop()` and `disconnect()`. Add this as an explicit method on
`MqttPublisher` (`publish_offline()` or similar) to keep the topic
string centralised.
**Severity:** MEDIUM

---

### A12. Per-node health state never distinguishes "no nodes ever healthy" from "all recovered"
**Concern:** operational visibility
**Scope:** `src/openwrt_presence/sources/exporters.py:33,72-83`
**Problem:** `self._node_healthy` starts empty. The first successful
scrape of a node inserts `True` — but that is not logged as a
"first_seen_healthy" transition, because `was_healthy =
self._node_healthy.get(node, True)`: the default is `True`, so the
code treats an unknown node as if it was already healthy. If a node
is unhealthy on the very first scrape, we log `node_unreachable`
correctly. If it is healthy on the very first scrape, no log. OK for
the normal case. But because the state is in-memory only, a container
restart during a long AP outage will log nothing at all ("was healthy
before the crash" assumption is lost) until the AP transitions. An
operator correlating logs across restarts sees silence where they
expect a "node still unhealthy on startup" signal.
**Impact:** operational blindness after restart. Not safety-critical
but impedes detection of stuck APs.
**Recommendation:** emit an `initial_node_state` info line after the
first `query()` per node, including whether it succeeded. Either that
or always log the first scrape result per node, unconditionally.
**Severity:** MEDIUM

---

### A13. No cap on `aiohttp` response size for `/metrics` scrape
**Concern:** resource exhaustion / malformed response handling
**Scope:** `src/openwrt_presence/sources/exporters.py:91-93`
**Problem:** `_scrape_ap` does `await response.text()` with no size
limit. A compromised or misconfigured AP (or a man-in-the-middle on
an HTTP-only endpoint — note `config.yaml.example` uses `http://`)
can return an arbitrarily large body. aiohttp will buffer it all into
memory. Combined with no content-type check, a surprise HTML error
page is matched against the regex and yields zero readings (fine),
but a gigabyte response will OOM the process.
**Impact:** a misbehaving AP can OOM the service, which restarts in
a loop, which for the safety-net 2-minute timeout window still
produces false departures (A2).
**Recommendation:** cap the response: `await response.content.read(1
<< 20)` or set a `read_bufsize`/`max_line_size` on the session.
Also check `response.status == 200` before parsing.
**Severity:** MEDIUM

---

### A14. `response.status` never checked; 500/503 treated as zero-readings
**Concern:** fail-secure
**Scope:** `src/openwrt_presence/sources/exporters.py:91-93`
**Problem:** `_scrape_ap` calls `response.text()` regardless of HTTP
status. A misbehaving AP serving a 503 with an HTML body returns zero
metric matches → zero readings → treated identically to "AP is
healthy and no one is home." The health transition logic therefore
marks the AP *healthy* (because no exception was raised) while in
reality the AP is returning an error page.
**Impact:** the engine will mark all devices on that AP as DEPARTING
even though the AP is technically reachable. `node_unreachable` never
fires. This is a subtle fail-unsafe path.
**Recommendation:** `response.raise_for_status()` inside
`_scrape_ap` or an explicit `if response.status != 200: raise
RuntimeError(...)`. Then the existing per-node health tracking
correctly classifies it as unreachable.
**Severity:** MEDIUM

---

## LOW

### A15. `_run` on SIGTERM can block up to ~5s on in-flight `source.query()`
**Concern:** shutdown latency
**Scope:** `src/openwrt_presence/__main__.py:92-110`
**Problem:** when the signal handler sets `stop_event` during an
`await source.query()`, shutdown waits for the query's aiohttp
`ClientTimeout(total=5)` per AP (in parallel, so ~5s worst case).
Not strictly a bug but adds SIGTERM-to-exit latency in some scenarios.
**Impact:** Docker will SIGKILL after 10s default if `source.query()`
is stuck. `source.close()` in `finally` still runs after that
timeout elapses.
**Recommendation:** if tighter shutdown is desired, wrap the poll
cycle in an `asyncio.wait` racing the query against `stop_event.wait()`
and cancel the query task when the event wins. Keeps shutdown under
one second.
**Severity:** LOW

---

### A16. `ConfigError` path produces an untagged traceback, not a structured log
**Concern:** error visibility
**Scope:** `src/openwrt_presence/__main__.py:22-24,116-120`
**Problem:** `Config.from_yaml` raises `ConfigError` at line 24 before
`setup_logging()` runs (which is on line 26). A structured logger is
not configured yet, so the traceback lands on stderr as plain text,
inconsistent with the rest of the service's JSON-on-stderr contract.
The monitor (`openwrt-monitor`) prints unparseable lines as-is — fine
— but log-shipping pipelines expecting JSON will choke.
**Impact:** log ingestion hiccup on config errors. Operator has to
read plain text. Not safety-critical.
**Recommendation:** call `setup_logging()` first, then load config
inside a try/except that `logger.exception("config_error", path=...)`
and re-raises. Alternatively produce a single structured line on
stderr manually before re-raising.
**Severity:** LOW

---

### A17. `asyncio.create_task` in `ExporterSource.query()` holds references via dict, OK — but no timeout on gather
**Concern:** task management
**Scope:** `src/openwrt_presence/sources/exporters.py:62-84`
**Problem:** the dict `tasks` holds strong references, so no GC
concern (good). Each task has an aiohttp `ClientTimeout(total=5)`, so
no runaway tasks. No issue; flagging to confirm it was checked.
**Impact:** none.
**Recommendation:** none needed. This is correct.
**Severity:** LOW (informational)

---

### A18. `main()` swallows KeyboardInterrupt silently; Ctrl-C in dev hides real errors
**Concern:** developer ergonomics / error visibility
**Scope:** `src/openwrt_presence/__main__.py:116-120`
**Problem:** `except KeyboardInterrupt: pass` at the top level drops
any stack trace. If the service is running interactively and raises
a `KeyboardInterrupt` from inside a finally block (because
`asyncio.run` cancels the task), the bare `pass` hides it. In
production it is moot (Docker uses SIGTERM), but in dev it makes
iteration harder.
**Impact:** minor ergonomic friction. Not safety-critical.
**Recommendation:** leave as-is, or log a debug line before pass.
Acceptable.
**Severity:** LOW

---

### A19. `_DeviceTracker` is never pruned, but bounded by tracked MACs
**Concern:** memory bounds (informational)
**Scope:** `src/openwrt_presence/engine.py:62,93-118`
**Problem:** `self._devices` only gains entries for MACs that pass
`_filter_tracked` in the source AND `mac_to_person is not None` in
the engine. So growth is bounded by the configured MAC count.
**Impact:** none.
**Recommendation:** none.
**Severity:** LOW (informational)

---

### A20. `StateChange.rssi` can be `None` but attributes JSON serialises it as `null`
**Concern:** HA entity attribute typing
**Scope:** `src/openwrt_presence/mqtt.py:113-123`
**Problem:** when a person has never been seen, `get_person_snapshot`
returns `rssi=None`, `mac=""`, `node=""`. `publish_state` then
publishes attributes JSON with `"rssi": null`. HA will accept this,
but HA template sensors consuming these attributes may break if they
do `{{ state_attr(..., 'rssi') | int }}` — `None | int` raises in HA
templates.
**Impact:** downstream HA automations are out of scope for this
review, but worth flagging for the integration side.
**Recommendation:** consider suppressing the attributes publish when
`rssi is None`, or using a sentinel (e.g. `-200`) so the JSON value
is always an int. Document the invariant on `StateChange`.
**Severity:** LOW

---

## Things I could not prove safe (flagging per the brief)

- **Out-of-order timestamp tolerance per CLAUDE.md commit `a8f7b6b`.**
  The engine uses `now` injected by the caller, not per-reading
  timestamps — `StationReading` has no `ts` field — so inter-AP clock
  skew cannot produce out-of-order reads at the engine layer. I
  could not locate code that explicitly handles this case and the
  quirk note refers to "processing-order tolerance." If the intended
  semantics is tolerance across *poll cycles* (later cycle has lower
  `now`?), the current code does not defend against it: `tracker.
  departure_deadline = now + timedelta(...)` would regress if `now`
  were ever non-monotonic. Recommend: assert `now >= self._last_now`
  or clamp to max seen. Severity: LOW unless clock regressions are
  a real concern in production.

- **Retained state contract on MQTT broker restart.** `on_connected`
  republishes cached state, which is correct only if the service has
  seen at least one state for each person. At boot, startup seed fills
  `_last_state` (per `publish_state` at `__main__.py:87`) so the
  cache is populated before any reconnect. Good — but depends on A1
  being fixed (otherwise the reconnect republish itself may crash).

- **paho thread interaction with `logger` / `structlog`.** structlog's
  `WriteLoggerFactory` writes to stderr. `_on_connect` and
  `_on_disconnect` call `logger.info`/`logger.warning` from the paho
  thread. Python's built-in I/O on `sys.stderr` is line-buffered and
  thread-safe at the `write()` level, and structlog renders per-call
  without shared state, so this should be safe. I could not exhaustively
  prove it. No recommendation beyond A1.
