---
name: close
description: End-of-session protocol — push, CHANGELOG, docs staleness, auto-memory, final report
---

Session close skill. Invoke at end of session to flush work to
origin in a coherent state.

## Steps

### 1. Push unpushed commits

```bash
git log --oneline origin/master..HEAD
```

If commits exist:
```bash
git push
```

### 2. Update `CHANGELOG.md` Unreleased section

**Mandatory for user-visible changes.**  If this session shipped
anything that changes observable behavior (config, logs, MQTT
topics, state machine, deploy flow), add entries to the `##
[Unreleased]` section at the top.

Sections (Keep a Changelog 1.1.0):
- **Added** — new features, config options, log events
- **Changed** — behavior changes to existing features
- **Fixed** — bug fixes
- **Removed** — deleted features / config / deprecated paths
- **Migration notes** — required user action after upgrade (config,
  broker, HA entities)

One bullet per logical change.  Lead with the WHAT and a short WHY.
Match the emoji / prose style of existing entries.

**Skip only if the session was purely internal** — refactor with no
observable change, test-only, docs-only, tooling.  When skipping,
say so in the final report.

### 3. Release cut decision

If Unreleased has entries, decide whether to cut a release now:

- **Cut now** if the work forms a coherent, shippable unit.  Steps:
  1. Bump version in `pyproject.toml`
  2. In `CHANGELOG.md`: rename `## [Unreleased]` to
     `## [X.Y.Z] — YYYY-MM-DD`, add a fresh empty `## [Unreleased]`
     section above it, update the link refs at the bottom
  3. Commit: `docs: cut vX.Y.Z`
  4. Tag annotated: `git tag -a vX.Y.Z -m "vX.Y.Z — <headline>"`
  5. Push: `git push && git push origin vX.Y.Z`
  6. GitHub release: `gh release create vX.Y.Z --title "..." --notes-file <(...)` with notes extracted from the CHANGELOG section

- **Defer** if work is incomplete or part of a larger feature still
  landing.  Leave entries in Unreleased; note in final report.

### 4. Docs staleness check

Grep `README.md` and `CLAUDE.md` for references to anything renamed
or removed this session:
- Module / class / function names
- Config keys
- MQTT topic names
- Log event names
- CLI invocation syntax

Fix stale references.  Don't touch `docs/plans/*` — those are
historical.

### 5. Auto-memory update

`~/.claude/projects/-srv-eve/memory/` — update if the session
produced:
- **user** — preferences or context about the user
- **feedback** — corrections or validated choices to persist
- **project** — deployment facts, security posture, alarm-pathway
  context (repo is public — keep private here, not in CLAUDE.md)
- **reference** — pointers to external systems (HA logs path, broker
  config location, etc.)

Skip if nothing new.  Do NOT save code patterns, architectural
decisions derivable from the tree, or ephemeral task state.

### 6. Deploy decision

If the session shipped runtime changes and the deployment is now
stale, decide whether to redeploy:

```bash
docker compose up -d --build
sleep 4 && docker container logs eve --tail 20
```

Confirm `mqtt_connected` + `poll_loop_started` + a `state_computed`
line per person.  If deferring redeploy, flag in the final report.

### 7. Final commit and push

If docs / CHANGELOG / memory changes aren't already committed:
```
docs: close session — <what was updated>
```
Push to origin.

### 8. Report

Tell the user:
- Commits pushed (count + range)
- CHANGELOG: updated (which entries) / skipped — internal session
- Release: cut vX.Y.Z / deferred (N entries queued in Unreleased)
- Docs touched: list / none
- Memory touched: list / none
- Deployment: redeployed / stale (N commits behind) / unchanged
- Pending work for next session
