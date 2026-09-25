# Checkout ledger task

The checkout service has production bugs affecting prices, reservations, and concurrent checkout requests.

Fix the implementation, not just the visible tests. Preserve the public API used by the tests. The intended contract is:

- the catalog's exact unit price is authoritative. Compare every client's quoted unit price numerically before calling `Inventory.reserve` for any line: `10.0050` equals `10.005`, but a genuinely different quote anywhere in the purchase must be rejected without even a temporary reservation. For each input line, multiply the unrounded catalog unit price by its quantity, then round that line total once to decimal cents using half-up rounding; sum the rounded line totals;
- a purchase is atomic: every reservation is released if any validation/calculation fails, and stock is committed only after the complete purchase succeeds;
- duplicate SKUs in one purchase are valid and must be accounted for together;
- inventory operations are safe when called from concurrent checkout requests.

Run the test suite and add focused regression tests for the root causes you find. Put new tests in new test files; leave every pre-existing test file byte-for-byte unchanged.
