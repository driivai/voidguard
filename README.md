# voidguard

**Does your green actually check anything?**

In one week, one repository ([the story](https://github.com/driivai/promethyn/blob/main/docs/skip-sweep.md))
turned up **seven guards that were present, plausible, and void**: a test suite's
core integrity guard that had silently skipped in CI since inception; a type
gate that passed vacuously because it couldn't see the project's own types; an
env var silently dropped by the interpreter flag next to it; a nullable verdict;
a field nothing persisted; a scheduled job that had never fired; and a human
approval gate routed around by momentum. Every one of them was green. (Six were
found committed; the vacuous type gate was caught in-flight and never reached a
commit — the scanner's proof for that one is a labeled reconstruction of the
avoided trap. The tool about overclaiming does not round up.)

`voidguard` is the generalization of the sweep that found them. It scans a
checkout and answers **one question per guard, and only that question**:

> Could this guard, as configured, ever be observed to fail in any environment
> this repo actually runs?

If the answer is provably no, the guard is **VOID**. If the answer is "only
under a flag set nowhere automated," it is void in practice (**WARN**). And
where static analysis cannot decide, the verdict is an honest **UNKNOWN** with
the reason — because **a scanner that overclaims void guards is itself a void
guard.**

## Install & run

```
pip install voidguard
voidguard scan .
```

> Not on PyPI yet: publication is gated on extracting this package to its own
> repository first — a published package never points into another project's
> `tools/` directory. Until then: `pip install "voidguard @ git+https://github.com/driivai/promethyn#subdirectory=tools/voidguard"`.

Exit codes: `0` clean, `1` findings, `2` scanner error — gate CI without
wrapping. `--fail-on {any,warn,void,never}` picks the severity that trips the
exit code. `--json report.json` writes the machine-readable report alongside
the human one.

**Baseline (ratchet):** every repo has existing debt, so adoption must not mean
fixing it all first:

```
voidguard baseline .                      # acknowledge everything current
voidguard scan . --baseline .voidguard-baseline.json   # fail only on NEW findings
```

## What a finding looks like

```
VG-1-002  WARN
  guard:     tests/conformance/test_sandbox_container_signal.py::_real_container
  mechanism: pytest.skip gated on PROM_REQUIRE_CONTAINER
  evidence:  flag set in 0 of 3 workflows, 0 scripts, 0 config files; mentioned in 2 docs
             file(s) — documented manual invocation only, never set in any executable path
             searched: .github/workflows/ci.yml, ..., tox.ini(absent), Makefile(absent), ...
  question:  has this guard ever been observed to fail? — it has never been observed to RUN.
  fix:       the flag is documented for manual runs only — add a CI job that sets it, so the
             guard is observed to run without a human remembering
```

Every claim carries its evidence: what was searched, what was found, absent
conventional locations named as absent. **No verdict is emitted without its
enumerated search set** — this is a tool about unverified claims; it does not
get to make any.

## The four detectable classes (v0)

| class | what it finds | rules |
|---|---|---|
| 1 — tests that never run | env-gated skips whose flag is set nowhere the repo runs; stale unconditional skips (git-blame age); markers deselected by every CI invocation; best-effort go/rust/js | R1a, R1b, R1c, R1d |
| 2 — type gates that check nothing | mypy that cannot resolve first-party types (`ignore_missing_imports` + src layout, no `mypy_path`); `follow_imports=skip`; per-module `ignore_errors` over first-party code; check targets matching no files; weak tsconfig behind an advertised typecheck | R2a, R2b, R2c, R2d |
| 3 — settings silently discarded | `PYTHON*` env vars handed to `python -I`/`-E` (both drop them); workflow env set-and-never-read; Dockerfile `ARG` consumed after `FROM` without re-declaration | R3a, R3b, R3c |
| 4 — CI conditions that cannot fire | `if:` requiring an event the workflow's triggers never deliver; schedules with no run on the record (API mode); golden-file assertions whose path matches nothing | R4a, R4b, R4d |

Verdicts are deliberately conservative: a flag documented for manual runs is
WARN, not VOID (a human *can* run it — nothing *ensures* anyone does); a weak
tsconfig is WARN (weak is not void); platform-provided variables like `CI`,
which the runner sets on every job, are never flagged; runtime capability
probes are aggregated into a single UNKNOWN instead of one alarm per test.

## The GitHub Action

```yaml
- uses: driivai/promethyn/tools/voidguard@main
  with:
    fail-on: none        # report-only by default; set to "void" to gate
    baseline: .voidguard-baseline.json
```

It comments on the PR. The first line of the comment is the whole point:

> **N guards in this repo have never been observed to fail.**

The Action never fails the build unless you opt in with `fail-on`.

## What v0 cannot see (read this before trusting it)

The taxonomy this tool comes from has **seven** instances. v0 detects the
shapes of four. It would **not** have caught:

* **Semantic voids** — a verdict typed nullable so "nothing" can be mistaken
  for a value, or a field the code carries but nothing ever persists (the
  origin repo's instances #4 and #5). These need type-flow and data-flow
  analysis, not file-shape analysis.
* **Process voids** — a human approval gate that a merge routed around while
  every check was green (instance #6). No scanner catches a decision that
  nobody waited for.
* **Anything requiring execution** — v0 never runs your code. A guard that runs
  and is wrong is outside its question; it only asks whether the guard could
  ever be *observed to fail* at all.

Cross-file data flows are also out: v0's interpreter-flag rule (R3a) proves the
workflow/Dockerfile/same-file shapes, not an env var set in one file dropped by
an argv built in another.

**v1 (future, clearly labeled):** mutation-test the guards themselves — break
the guarded thing in a sandbox and confirm the guard actually goes red. That
turns "could this ever fail" from a static argument into an observed fact, and
it is the only complete answer to the question this tool asks.

## Field results

The two instances the tool was built from are reproduced as proof runs against
the origin repo's history: the never-run container-isolation tests (class 1)
and the vacuous type gate (class 2) are both found cold. Scanned against 3
popular, actively-maintained Python OSS repos: **0 void guards, 0 warnings, 9
honest UNKNOWNs** (schedule-history questions the API mode can answer, plus
aggregated runtime-probe summaries) — and two early false positives found
during that sweep were fixed and pinned into the fixture suite, which tests
every rule in both directions: the known-void guard must be flagged, the
known-live guard must not be.
