# Task 3 report: session-bound truth across restart and session rotation

## Implementation

- Reconciled `cfg.commission_perc` from a loaded same-session `PaperLedger` before `DemoExchangeClient`, strategy, and `RuntimeConfigSynchronizer` construction. The persisted ledger therefore remains authoritative across a worker restart while a newly desired commission stays pending.
- Seeded a newly created Demo ledger from the effective `cfg.demo_inv_initial` value. This allows a fresh session to consume the desired inventory together with `cash_initial` and `commission_perc`.
- Did not change `PaperLedger`, Algorithm 1, or lifecycle/control-agent behavior.

## Tests and TDD evidence

### RED

- `python3 -m pytest -q market_maker/tests/test_runtime_config_sync.py::test_same_session_restart_keeps_commission_pending`
  - Failed as intended: restarted `cfg.commission_perc` was `0.02`, while the stored same-session ledger commission was `0.001`.
- `python3 -m pytest -q market_maker/tests/test_runtime_config_sync.py::test_new_session_consumes_pending_inventory_cash_and_commission`
  - Failed as intended: the new ledger started with inventory `0.0` instead of desired inventory `7.0`; desired cash `900.0` and commission `0.02` were already consumed.

### GREEN

- The two Task 3 regression tests passed together: `2 passed in 0.83s`.
- Required focused suite:
  - `python3 -m pytest -q market_maker/tests/test_runtime_config_sync.py market_maker/tests/test_demo_safety.py market_maker/tests/test_demo_engine_lifecycle.py`
  - Result: `37 passed in 1.30s`.
  - The exact cached tree was independently materialized with `git checkout-index` and passed the same suite: `37 passed in 1.56s`.
- Full suite:
  - `python3 -m pytest -q`
  - Result: `392 passed, 12 failed, 1 warning in 67.72s`.
  - Six failures are the known Telegram lifecycle baseline. The other six are outside Task 3: three legacy Admin HTML/config expectations, two control-agent calls that have not adopted Task 1's strict pair-aware `write_overrides` interface, and one runtime-path stub that has not adopted Task 2's initial config publication. These belong to later Admin/control lifecycle integration rather than the session-bound worker truth implemented here.

## Files and scoped hunks

- `market_maker/run.py`
  - One line reconciles loaded-ledger commission into `cfg`.
  - One line seeds a new ledger from `cfg.demo_inv_initial`.
  - Both hunks were interactively staged; unrelated pre-existing fill-budget removals in this dirty file remain unstaged.
- `market_maker/tests/test_runtime_config_sync.py`
  - Added a lease-aware runtime-bot helper.
  - Added same-session restart and new-session consumption regressions.
- `.superpowers/sdd/task-3-report.md`
  - This report.
- `market_maker/paper.py` was not modified for Task 3; its pre-existing user edits remain unstaged.
- `market_maker/tests/test_demo_engine_lifecycle.py` required no Task 3 hunk; its focused suite was run unchanged.

## Self-review

- The loaded ledger's session and symbol are validated before its commission is trusted.
- Reconciliation happens before every consumer named by the brief: the exchange facade, strategy, engine, and runtime synchronizer/snapshot.
- Inventory and cash are not rewritten on an existing ledger. Runtime applied values continue to come from the loaded ledger account state.
- New-session behavior uses the desired Demo-specific inventory field directly and leaves real-mode construction unchanged.
- No Algorithm 1 or Task 4 lifecycle/control-agent code was changed.
- Cached diff was inspected after partial staging; it contains only the two Task 3 production lines and Task 3 tests (plus this report when forced-added).

## Concerns

- The full suite currently has six additional non-Telegram integration failures introduced by the strict pair-aware/initial-publication interfaces from Tasks 1 and 2. They are intentionally not fixed here because the brief excludes Task 4 lifecycle/control-agent work.
- `run.py` and `paper.py` remain dirty from unrelated user work. The Task 3 commit uses only the explicitly staged `run.py` lines; the unrelated working-tree changes remain in place.
