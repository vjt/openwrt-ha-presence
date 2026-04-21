---
name: start
description: Session start protocol — release state, git state, deployment status, what's available
---

Session start skill. Run through the checks and produce a status
report. The "what's pending" dashboard for eve.

## Steps

### 1. Release state

Inspect `CHANGELOG.md`:
- If `## [Unreleased]` is empty → clean, nothing to ship.
- If `## [Unreleased]` has entries → flag them as **pending release**.
- Compare the latest numbered entry (`## [X.Y.Z]`) against the highest
  tag on origin (`git ls-remote --tags origin | awk '{print $2}' |
  grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1`).  If the
  CHANGELOG has an entry newer than the highest pushed tag, that's a
  release that was committed but never cut.

### 2. Git state

```bash
git status
git log --oneline origin/master..HEAD
git log --oneline -5
git stash list
git worktree list
```

Note: uncommitted changes, unpushed commits, stash entries, worktrees.

### 3. Deployment state

```bash
docker container inspect eve --format \
  '{{.State.Status}} since {{.State.StartedAt}}, image {{.Image}}'
```

Compare the running container's build time / image ID against the
latest commit on master. If the image predates current `master`, the
deployment is stale.

`docker container logs eve --tail 20` for a quick sanity check — look
for recent `mqtt_connected`, `poll_loop_started`, and any `ERROR` /
`WARNING` lines.

### 4. CI / test sanity

```bash
.venv/bin/pytest -q
```

If the venv isn't primed, skip — the deployed container proves
runtime validity. Note any failures as blockers.

### 5. Produce the report

Format:

```
📦 **Release State**: clean / UNRELEASED: vX.Y.Z in CHANGELOG, not tagged
🌿 **Git State**: clean / N uncommitted / N unpushed / stash entries / worktrees
🐳 **Deployment**: eve running since TIMESTAMP, image matches / STALE (N commits behind)
🧪 **Tests**: N passed / FAILING

## What's Available
<given the state, what we can work on now: finish a pending release,
 ship uncommitted work, investigate production issue, start new
 feature, etc.>
```

The **What's Available** block is the main output. If something is
mid-flight (unreleased entries, unpushed commits, stale deployment),
surface that before proposing new work.
