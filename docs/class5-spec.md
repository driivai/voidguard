# Class 5 — vacuous assertions (EXPERIMENTAL, not in the default scan)

Status: **recon prototype**. Run explicitly with `python -m voidguard.rules_class5 <path>`.
It is not registered in the engine, not reachable from `voidguard scan`, and not part
of the published package's behavior until the go/no-go (docs/class5-recon.md) says go.

## The refined question

Classes 1–4 ask: *could this guard ever be observed to fail?* A test with no real
assertion **can** fail — the code under test can raise — so Class 5 has to sharpen
the question or it would claim too much:

> Does this test verify any behavior beyond "the code did not raise"?

A test that only proves executability is a smoke test. Smoke tests have value, but a
suite of them reporting "N passed" claims a verification level it does not deliver.
Class 5 flags tests whose *assertion layer* is absent, tautological, or decoupled
from the code under test — and says exactly which, with the evidence.

## The shapes

### S1 — no assertion at all (detectable: YES · verdict: VOID)

The test body contains no `assert` statement, no `unittest` assertion method, no
`pytest.raises`/`pytest.warns` context, no mock assertion, and no call that resolves
to an asserting helper. It verifies only that the code does not raise.

FP risk — **the whole game**, all of these are NOT vacuous and must not flag:

- assertion via a **helper function** (`check_roundtrip(x)` where the helper asserts).
  Mitigation: any called name matching `assert*|check*|verify*|expect*|validate*` is
  treated as a verifier; calls to helpers *defined in the same file* are resolved one
  level deep and count if their body asserts. Unresolvable helper calls downgrade the
  test to UNKNOWN, never VOID.
- assertion via a **fixture** (a yield-fixture that asserts after `yield`).
  Mitigation: fixtures defined in the same file with post-yield asserts mark their
  users as verified; conftest-defined fixtures are resolved when conftest.py is in
  the scanned tree; otherwise the test is UNKNOWN, not VOID.
- **pytest.raises / pytest.warns / assertRaises** — the exception IS the assertion.
- **`@pytest.mark.xfail`** — the pass/fail contract is inverted; never flag.
- **snapshot/approval frameworks** (`snapshot`, `approvals`, `verify(...)`) — treated
  as verifiers by the name heuristic.
- **doctests** are out of scope entirely (different mechanism, not scanned).

### S2 — only tautological assertions (detectable: YES · verdict: VOID)

Every assertion in the body is statically true: `assert True`, `assert 1`,
`assert "literal"`, `assert x == x` (identical AST on both sides), `assert x is x`,
`assertTrue(True)`. The assertion layer exists and cannot fail.

FP risk: low — the tautology must be *every* assertion in the test; one real
assertion clears the test. `assert x == x.copy()` is not a tautology (different AST).
Deliberate `assert True  # placeholder` markers are true positives, not FPs.

### S3 — only-truthiness on a computed value (detectable: YES · verdict: WARN, not VOID)

Every assertion is a bare truthiness check on a call result (`assert result`,
`assert obj.method()`). This CAN fail (None/0/empty/False), so it is weak, not void —
same reasoning as the weak-tsconfig rule: **weak is not void**. WARN.

FP risk: moderate. `assert items` after a filter is often exactly the right
assertion. WARN's wording says "weak," never "verifies nothing." Bare truthiness on
a *boolean-returning* call (`assert is_valid(x)`) is a real verification — names
matching `is_|has_|can_|should_` are exempted, as are boolean predicates by
convention (`any`, `all`, `isinstance`, `re.match/search`, `path.exists`, …).
The `assert any(...)` exemption was added after the very first dogfood run
false-positived on a real test — recorded here because that is the recon working.

### S4 — function under test fully mocked (detectable: MOSTLY NO · v0: not attempted)

If the subject is patched out, assertions check the mock, not the code. Deciding
"fully mocked" statically requires knowing the import graph of the subject — which
the test file alone does not carry. v0 does not attempt this shape; the spec records
it so nobody mistakes silence for coverage. The narrow observable sub-case is S5.

### S5 — asserts interactions only, never a value (detectable: PARTIAL · verdict: UNKNOWN)

Every assertion is of the form `mock.assert_called*` / `call_count` / call-args
inspection — the test pins *that something was invoked*, never *what was produced*.
For adapters and notification paths this is legitimate design; statically we cannot
know. UNKNOWN, with the evidence naming every interaction-assertion found.

### S6 — the name claims a behavior the body never exercises (detectable: BEST-EFFORT · verdict: UNKNOWN)

`test_rejects_negative` with no `raises` context and no negative literal anywhere in
the body; `test_raises_on_empty` with no `raises`/`assertRaises`. Name semantics are
a convention, not a contract — UNKNOWN-leaning by design, and only the narrow
`rejects|raises|errors|invalid` vocabulary is attempted. `fails?` was in the first
draft and immediately false-positived on a real test named after a `--fail-on` CLI
flag; it is dropped, and that decision is a datum: name heuristics burn trust fast.

FP risk: high by nature (a name can be sloppy while the test is sound). That is why
this shape can never produce VOID and its evidence quotes both the name token and
what was searched for in the body.

## Verdict discipline

- VOID requires the *assertion layer* to be provably absent or provably unable to
  fail, after helper/fixture resolution. Anything unresolved is UNKNOWN.
- WARN means "can fail, but the check is weaker than the test's existence implies."
- UNKNOWN is first-class and carries the reason, same as every other class.
- Every finding enumerates its search set: which assertion mechanisms were looked
  for, which helpers were resolved (or could not be), which fixtures were examined.

## Known blind spots (recorded, not hidden)

- Helpers imported from another module are not resolved (→ UNKNOWN, by policy).
- Class-based fixtures/setUp assertions across files.
- Property-based tests (hypothesis) — `@given` bodies are exempted from S3 (bare
  truthiness there is often the property itself).
- `# type: ignore`-style intent comments are not read; the AST is the contract.
- Async tests are handled the same as sync (ast.AsyncFunctionDef).
