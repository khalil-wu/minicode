# Repository task evaluations

These manifests measure whether an agent can repair or refactor a real isolated
repository. They are not prompt-unit tests and they do not use an LLM judge.

Each JSON manifest declares:

- `source`: either a local repository/fixture or a Git URL plus an exact
  40-character commit SHA; Git sources are fetched into a reusable cache and
  checked out detached in a disposable workspace;
- `baseline_patch`: an optional trusted test patch applied before the broken
  baseline is sealed (used by the SWE-bench catalog);
- `setup`: commands required to prepare dependencies or inject the defect;
- `baseline`: checks that must fail before the agent runs;
- `judges`: compile, test, typecheck, behavior-diff, migration, or security checks
  that must pass after it runs;
- `agent_verify_command`: the focused mechanical check fed back into MiniCode's
  own loop after a mutation, so a failing implementation can be repaired before
  the external judges run;
- `file_judges` and `forbidden_changes`: structural invariants that prevent
  deleting tests, rewriting fixtures, or escaping the intended change scope.
- `ignored_changes`: narrow runner-owned artifacts (plans and TODO state by
  default) excluded from the production diff without ignoring other
  `.minicode` files.
- `seeds` and `minimum_pass_rate`: independent agent runs and the mechanical
  aggregate threshold. The seed is passed as `MINICODE_EVAL_SEED`.

Commands are argv arrays rather than shell strings. The prompt is supplied to
the agent driver through stdin, while `MINICODE_EVAL_WORKSPACE` and
`MINICODE_EVAL_TASK_ID` identify the isolated checkout.

Example:

```powershell
python scripts/run-agent-evals.py evals/repository-tasks/tasks/my-task.json `
  --agent-command python path/to/minicode_agent_driver.py `
  --keep-workspace
```

The runner writes a JSON evidence report for every seed plus an aggregate
report for every task. A seed only passes if the broken baseline is proven, the
agent exits successfully, the workspace has an allowed diff, and every
mechanical judge passes. The task passes only when its configured pass-rate
threshold is reached.

`swebench-lite/catalog.json` indexes 20 fixed-revision tasks imported from the
official SWE-bench Lite records. Each task includes the original issue, test
patch, FAIL_TO_PASS judge, a bounded PASS_TO_PASS regression sample, three
seeds, and a two-of-three pass threshold. Regenerate the checked-in catalog
from a verified dataset export with:

```powershell
python scripts/import-swebench-evals.py --dataset artifacts/swebench-lite.json
```
