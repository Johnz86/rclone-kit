# Code style

Follow KISS, DRY, and SOLID principles; use clean naming conventions and
functional programming where it makes the implementation clearer. Optimize
for programmer happiness and convention over configuration. The project has
an omakase philosophy: provide a curated, integrated system of tools that work
best together instead of requiring developers to assemble every component.

Inline comments often indicate that code should be decomposed into smaller
functions with docstrings. Replace magic strings with descriptively named
uppercase constants. Use Python 3.13 features and precise types.

## Testing strategy

The examples below demonstrate the intended code style rather than this
project's storage domain.

### Test inputs as named constants, not factory functions

Reusable domain objects (models and value objects) shared across tests live
in `tests/helpers.py` as module-level constants. Import only what the test
needs.

```python
# tests/helpers.py
REUSED_OBJECT_INSTANCE = UsedOrmExampleModel(
    domain_argument="string_value",
    second_enum=DataEnumExample.ONE_VALUE,
)

# tests/test_functionality.py
from helpers import REUSED_OBJECT_INSTANCE
```

### Parametrized test cases as frozen dataclasses

For parametrized tests with multiple fields, define a
`@dataclass(frozen=True)` for the case shape. Each case is a named
module-level constant; the name documents intent and replaces an inline
comment.

```python
@dataclass(frozen=True)
class EvalCase:
    predicted_topic: str
    predicted_stance: str
    targets: list[TopicStanceModel]
    expected_key: float
    expected_value: float
    expected_query: float


DOMAIN_EXAMPLE_EXACT_MATCH = EvalCase("example_topic", "support", [TRUMP_SUPPORT], *LIKE)
DOMAIN_EXAMPLE_MISMATCH = EvalCase("example_topic", "oppose", [TRUMP_SUPPORT], *SKIP)

EVAL_CASES = [DOMAIN_EXAMPLE_EXACT_MATCH, DOMAIN_EXAMPLE_MISMATCH]


@pytest.mark.parametrize(
    "case",
    EVAL_CASES,
    ids=["domain_exact_match", "domain_mismatch"],
)
def test_evaluate(case: EvalCase):
    ...
```

Rules:

- **No `label` field** on case dataclasses — the constant name is the label.
- **No `make_*_cases()` factory functions** — use a plain constant list
  collecting the named cases.
- **No inline comments** next to case definitions — if the constant name
  does not explain the case, rename it.
- **Tuple constants** such as `EXAMPLE_VALUE = (1.0, 1.0, 0.0)` are fine
  for repeated expected-value groups and can be unpacked with `*`.
- **`ids` as a string list** matching the constant names in snake case,
  passed to `@pytest.mark.parametrize`.

### When not to parametrize

Use a plain test function when there is a single scenario or when cases
differ in control flow, not just data. Parametrize only when the test body
is identical across cases.

### Use production enums and constants in tests

Never hardcode magic strings in tests.
Import data structures from production code to construct test data.
Tests should break when enum values change.

### Test file structure

Each test file follows this order:

1. Imports (stdlib, third-party, project, helpers)
2. Fixtures
3. Case dataclasses, named case constants, case lists
4. Parametrized test functions
5. Standalone test functions

## Commit rules

- **Author identity**: `Jan Jakubcik <jakubcikjan@gmail.com>` for every
  commit — no `Co-Authored-By` trailer and no other name or email.
- **One logical change per commit.** Unrelated changes, such as a
  documentation rewrite and a test fix found during that work, belong in
  separate commits.
- **Imperative subject line** such as "Fix flaky directory-order assertion
  in test_os_walk", under roughly 70 characters with no trailing period.
- **Body explains why, not what.** The diff already shows what changed. The
  body records reasoning a reviewer cannot infer from the diff.
- **Never commit or push without being explicitly asked**, even after
  finishing implementation work. Surface the diff and wait.
