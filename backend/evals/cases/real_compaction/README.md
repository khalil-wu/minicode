# Real task: long-session compaction

This case recreates the MiniCode failure fixed in commit `2845fe14` on a clean snapshot of commit `e658ffa7`. `prepare.py` restores the three affected source files from before that fix, keeps the regression test in its post-fix form, removes the explanatory fix report, and creates a one-commit task repository with no solution history.

Give the agent only `prompt.md` and the prepared task checkout. Run `oracle.py` separately from the task checkout after the agent finishes. The oracle checks three independent boundaries: a successful compaction does not terminate the turn on an estimate, reported provider usage can correct that estimate downward, and the next provider request is admitted after compaction.

The seed fails all three external checks; the reference workspace passes all three. The task checkout also has one visible failing regression test. The case is based on a measured long-session failure, rather than a fabricated source change.
