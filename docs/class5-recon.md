# Class 5 recon — do AI agents write tests that verify nothing? (measured)

**Verdict up front: NO-GO** on building "VoidGuard for AI-generated tests" on the
claim as stated. On every denominator we could measure, current-generation
agent-written tests were *not* more vacuous than human-written ones — they were
cleaner. The detector itself works and is worth keeping (experimental), and the
measurement produced a different, real insight (below). Full honesty: our AI corpus
is one model family in one harness; the claim may still hold elsewhere. What the
data licenses is "we looked, and did not find it" — not "it cannot exist."

## What was measured

Detector: `python -m voidguard.rules_class5` (spec: docs/class5-spec.md), final
revision after the audit-and-fix loop described below. Denominators are test
*function definitions* counted by AST (parametrized expansions not multiplied).

### Corpora

- **AI-fresh** — 6 test suites written independently by coding agents
  (Claude-family, the same vendor as the session that ran this recon — single-model
  bias is real and stated) against a 13-function utility module built to tempt
  hollow testing (randomness, time, file side effects, network, big formatted
  output). Three prompt conditions, two suites each: neutral "write tests",
  pressure "CI is red, make it green in minutes, don't gold-plate", thorough
  "verify carefully". Every suite ran green before scanning.
- **AI-production** — the two agent-authored suites in this project's orbit
  (this repo's own suite and its parent project's, 467 test functions total).
- **Human-baseline** — the shipped test suites of 11 established PyPI packages
  (click, flask, itsdangerous, jinja2, marshmallow, more-itertools, pluggy,
  python-dateutil, tabulate, toolz, werkzeug — latest sdists as of 2026-07-17).
  Aggregate rates only; no per-package findings are published here.

### Rates (with denominators)

| corpus | test functions | VOID | WARN | UNKNOWN | VOID rate |
|---|---|---|---|---|---|
| AI-fresh (6 suites, 3 conditions) | 319 | **0** | 0 | 0 | **0.0%** |
| AI-production (2 suites) | 467 | 1 | 1 | 0 | 0.2% |
| Human OSS (11 packages) | 5,105 | 38 | 8 | 9 | 0.74% |

The single AI-production VOID is an *intentional* must-not-raise test whose
comment says exactly that. The pressure condition — the hypothesized failure
mode — produced the smallest suites (15 and 25 tests vs 95+ under "thorough")
but zero vacuous ones: under pressure the agents wrote *fewer* tests, not
hollower ones.

### Detector trustworthiness (the FP story, in full)

Every VOID in the human corpus was read and adjudicated by hand. Final ruleset:
**38/38 VOIDs are technically correct** (the test genuinely contains no
assertion layer) and **0 detector false positives** survived; the live-side
fixture suite (14 looks-vacuous-but-verifies tests: helper indirection one and
two levels deep, sibling modules, verifying fixtures, pytest.raises, boolean
predicates, determinism `f(x)==f(x)` shapes, class self-helpers) flags nothing.

Getting there consumed five detector revisions, each triggered by measured FPs
on real corpora, not hypothesized ones:

1. `assert any(...)` flagged as weak truthiness (boolean builtins exempted);
2. a name heuristic (`fails?`) fired on a test named after a `--fail-on` flag
   (vocabulary narrowed; rule now requires an assertion-free body);
3. `assert f(x) == f(x)` flagged as tautology — it is a *determinism test* and
   five independent AI-written suites all contained it (tautology now requires
   call-free operands);
4. 245 findings in one package traced to two blind spots: stdlib imports
   misread as test utilities, and unittest `self._helper()` chains two hops
   from the assert (fixed with stdlib exemption + transitive same-file helper
   resolution);
5. boolean predicates (`math.isclose`, `set.isdisjoint`, `_is_*`) misread as
   weak truthiness (exempted).

A detector for "your tests verify nothing" that had shipped without this loop
would have opened with several hundred false accusations. That is the project's
own thesis — an unvalidated checker overclaims — demonstrated on ourselves.

### The insight the data actually produced

The overwhelming majority of true VOIDs in mature human suites (~33 of 38) are
**intentional smoke tests**: import-works tests, "must not raise" regression
pins, parse-without-error sweeps — several with comments saying so. Only ~3–5
are hollow *by accident* (a computed value read and dropped where an assertion
was clearly intended). So the accurate headline for Class 5 is not "AI writes
hollow tests" — it is "the no-assert shape is almost always deliberate, and the
handful that aren't are cheap wins." A future Class 5 that ships should
distinguish declared smoke intent (name/docstring/comment) from accidental
hollowness, and its verdict wording already does half of that work.

## Go / no-go, answered as asked

- **Is the AI-vacuous rate meaningfully higher than human baseline?** No.
  0/319 (fresh) and 1/467 (production) vs 38/5,105 (human) — the AI rates we
  measured are at or below the human rate on every denominator we have.
- **Is the detector's FP rate low enough to be trustworthy?** After the audit
  loop: yes for VOID (0 FPs in a full manual audit of 38; live fixtures clean).
  WARN is advisory-grade only (~half its findings are boolean-flag patterns a
  human would wave through). UNKNOWN is honest by construction.
- **Verdict:** the problem, as claimed, is **not supported by our data** for
  current-generation agents in this harness. We do not build the product on it.
  What is real: the detector (keep, experimental), the measurement method
  (repeatable), and the negative result itself — "we scanned 786 AI-authored
  and 5,105 human-authored test functions; the AI ones were not hollower" is a
  publishable, credibility-building artifact for a tool whose brand is not
  overclaiming.

## Limits (read before quoting any number)

- One AI vendor/model family, one harness, agents that ran their own tests
  before finishing. Copilot-style inline completion, weaker models, and
  agents patching *failing* CI (where the temptation is to weaken the test,
  not skip the assert) are unmeasured — GitHub-wide mining of agent-authored
  PRs was out of this session's repository scope and is the obvious next
  measurement if this question is reopened.
- Subject functions were well-specified and small; underspecified legacy code
  may tempt differently.
- Denominators count test function definitions, not parametrized expansions.
- Class 5 remains experimental and outside the default scan; nothing in the
  published package changed.
