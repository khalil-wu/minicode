# Real task: scheduled work across restarts

This case reintroduces the scheduler defects fixed in commit `8cac33e5` onto a clean snapshot of commit `e658ffa7`. The task checkout contains one baseline commit and no fix history. The later `tests/test_scheduler_integrity.py` file is kept outside the agent checkout as an oracle source.

The external oracle checks four user-visible boundaries: deleting the last workspace task must stay deleted after restart, pending/running cleanup owners survive history trimming, expired or invalid persisted schedules do not fire, and local recurring times use the offset at the future scheduled instant. The seed must fail these checks; the reference workspace must pass them.
