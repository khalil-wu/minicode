# Checkout ledger benchmark

Copy only `task/` into an isolated workspace and give its `README.md` to the agent. Keep `oracle/` outside that workspace. The task starts from baseline commit `0716a90` of the local checkout-ledger fixture; the task text makes quote validation and per-line rounding explicit.

After the agent finishes, run the visible tests from its workspace, then run `oracle/test_regressions.py` with that workspace on `PYTHONPATH`. The external oracle uses only the public `Checkout`, `Inventory`, and pricing APIs. It checks the specified behavior without exposing hidden assertions to the agent.

The unmodified task passes 8 of 10 external assertions. A reference implementation has passed 10 of 10. The concurrency assertion was repeated 30 times against that reference without a failure.
