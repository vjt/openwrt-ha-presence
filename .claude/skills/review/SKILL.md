---
name: review
description: Dispatch parallel codebase or architecture review agents. Require argument
---

Full-codebase review. **Requires argument**: `architecture` or `codebase`.
No default. If invoked without argument, ask which one.

Small codebase (~975 LOC src, 8 files). 4 parallel background agents,
one per concern. Each reads the whole source tree through its lens.
This is deliberately fewer than gastone's 6 — a per-directory split
would be redundant at this scale.

## Argument: `architecture`

Structural review. Asks "is this module shaped right?" not
"is this line correct?"

### Dispatch — 4 parallel background agents

| Agent | Concern |
|-------|---------|
| A1. Boundaries & responsibilities | Abstraction leaks, cohesion, dependency direction, interface contracts, god-objects, wrong-layer logic |
| A2. Type system & correctness | Stringly-typed data, `dict[str, Any]` leaks, `Any` in public sigs, missing enums / NewType / discriminated unions, frozen-vs-mutable consistency, MAC normalization enforced by convention vs types |
| A3. Robustness & failure modes | Fail-secure for alarm pathway (dead AP → false AWAY?), async + paho-thread interaction, signal handling, shutdown ordering, reconnect races, LWT / retained contract, `except Exception` patterns, clock skew tolerance |
| A4. Extensibility & test architecture | Cost of adding a source / publisher / state / person; config sprawl; tests asserting outcomes vs mock-call sequences; fixture reuse (`sample_config`); time injection compliance; safety-path coverage |

Each agent MUST read:
- `CLAUDE.md` (the engineering standards — alarm-critical context lives there)
- Every `.py` file under `src/openwrt_presence/`
- All test files when relevant (A4 mandatory)

### Agent prompt template (common)

Report **architectural findings only**, not line-level bugs. Format:

```
### A{N}. Short title
**Concern:** which lens
**Scope:** modules / files / lines
**Problem:** structural issue
**Impact:** what breaks, drifts, or gets harder (for A3: who notices?)
**Recommendation:** concrete path forward
**Severity:** CRITICAL / HIGH / MEDIUM / LOW
```

Severity:
- **CRITICAL** — blocks correctness or safety (especially alarm pathway)
- **HIGH** — significant maintenance burden or latent bug class
- **MEDIUM** — tech debt
- **LOW** — improvement opportunity

What agents ignore: style, performance (unless caused by structure),
test-count percentages, pre-existing known issues.

Output path: each agent writes its findings to
`docs/reviews/architecture/_agent_a{N}_<slug>.md` (underscore prefix
= intermediate, to be merged).

## Argument: `codebase`

Line-level scan. Same 4-agent split, different lens per agent:

| Agent | Scope |
|-------|-------|
| C1. engine + config | Pure-logic core. State-machine invariants, default args, typed signatures, `| None` honesty |
| C2. mqtt + __main__ | I/O wiring. LWT / retained / QoS, reconnect, shutdown ordering, signal handling, startup seed |
| C3. sources + logging + monitor | Scrape correctness, regex fragility, DNS / session handling, JSON log schema, ANSI colors |
| C4. tests + cross-cutting | Mocks vs outcomes, fixture reuse, time injection, CLAUDE.md adherence across all files |

Each agent MUST read `CLAUDE.md` + its scope + sibling modules it
references. Report **problems only**:

```
### C{N}. Short title
**File:** `path:line`
**Category:** dead-code / default-args / types / exceptions / leak / duplication / standards
**Severity:** CRITICAL / HIGH / MEDIUM / LOW
Description.
**Fix:** Concrete suggestion.
```

What to look for:
- Dead code (unused functions, imports, variables, unreachable branches)
- Default arguments (`= None`, `= []`, `= {}`) outside `timeout=30`-style config defaults
- Untyped / weakly-typed (`dict[str, Any]`, bare `dict`, `Any`, missing types)
- Optional fields that are never actually None in practice
- Abstraction leaks (raw dicts/tuples callers must parse)
- Swallowed exceptions (`except Exception: pass`, log-and-continue where re-raise fits)
- Stale patterns contradicting `CLAUDE.md`
- Security-critical violations (alarm pathway: never default to "home",
  retained MQTT contract, LWT, every state change logged)

What to ignore: style preferences, test-file internals (unless
asserting on mocks instead of outcomes).

## After all agents complete

1. Read each `_agent_*.md` file.
2. Deduplicate findings (A1 boundaries overlap with A4 extensibility
   is common).
3. Compile into a single review document:
   - `docs/reviews/architecture/YYYY-MM-DD-architecture-review.md`
   - `docs/reviews/codebase/YYYY-MM-DD-codebase-review.md`
4. Structure the compiled document:
   - **Summary** — one paragraph: what was reviewed, overall health
   - **Critical findings** — must-fix before next release
   - **High findings** — should-fix soon
   - **Medium / Low** — tech debt backlog
   - **Per-agent appendix** (optional) — preserve raw agent reports
   - **Severity table** — counts by concern
5. Delete `_agent_*.md` intermediate files.
6. Present top 3–5 findings + severity counts to the user.
   Do NOT auto-fix. Review is diagnosis, not treatment.

## Notes

- Always use `run_in_background: true` for all 4 agents — they run
  for several minutes each and are independent.
- Agents get isolated context; the user-facing session stays clean.
- If this is the first review, `docs/reviews/{architecture,codebase}/`
  may not exist — create it.
- Reviews are tracked artefacts. Don't delete old ones; they're the
  trajectory record.
