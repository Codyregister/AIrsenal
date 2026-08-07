"""
Regression tests for a real hang observed while replay-testing season 2223:
a bug in SquadOpt (see test_optimization_squad.py) meant a wildcard/free-hit
node's optimization could raise partway through a tree-search worker's
processing of one queue item. That worker died silently; every other worker
in the pool then spun forever (`time.sleep(5)` loop) because their
termination check compared completed output files on disk to a precomputed
expected total that could no longer be reached - the crashed node's subtree
was never going to produce its outputs. `optimize()` in
fill_transfersuggestion_table.py now (a) catches exceptions per tree node
instead of letting them kill the worker, and (b) tracks completion via a
shared "outstanding work" counter instead of a precomputed static total, so
an abandoned subtree just shrinks the total amount of work rather than
making it permanently unreachable.

These tests exercise that resilience/termination logic directly, with a
fake queue and a stubbed-out node processor, rather than via real
multiprocessing (too slow/flaky for a unit test, and not what changed here -
see test_chip_strategy_integration.py for why the surrounding
run_optimization tests take the same approach).
"""

import airsenal.scripts.fill_transfersuggestion_table as fts
from airsenal.framework.multiprocessing_utils import SharedCounter


class _FakeQueue:
    """Minimal stand-in for CustomQueue: a plain FIFO list with qsize()."""

    def __init__(self, items):
        self._items = list(items)

    def qsize(self):
        return len(self._items)

    def get(self):
        return self._items.pop(0)


def test_optimize_survives_a_node_that_raises_and_still_terminates(monkeypatch):
    """A node that raises should be logged and abandoned, not crash the
    worker - and the worker should still exit once every other node has
    resolved, rather than looping forever."""
    processed = []

    def fake_process_strategy_node(
        status,
        queue,
        pid,
        gameweek_range,
        season,
        pred_tag,
        chips_gw_dict,
        outstanding,
        **kwargs,
    ):
        processed.append(status)
        if status == "boom":
            # simulate the real failure mode: an exception partway through
            # processing, before this node's own outstanding.increment(-1)
            msg = "simulated SquadOpt-style crash"
            raise RuntimeError(msg)
        # simulate a resolved leaf node (no children queued)
        outstanding.increment(-1)

    monkeypatch.setattr(fts, "_process_strategy_node", fake_process_strategy_node)

    items = ["ok-1", "boom", "ok-2"]
    outstanding = SharedCounter(len(items))
    queue = _FakeQueue(items)

    # should return (not hang) despite the "boom" item raising
    fts.optimize(queue, 0, [1], "9999", "tag", {}, outstanding)

    assert processed == ["ok-1", "boom", "ok-2"]
    # the abandoned node's subtree still gets accounted for (via optimize's
    # except-block decrement), so outstanding correctly reaches zero
    assert outstanding.value == 0


def test_optimize_terminates_immediately_when_no_work_queued(monkeypatch):
    """With outstanding already at zero and an empty queue, the worker
    should return straight away instead of ever sleeping/polling."""
    monkeypatch.setattr(
        fts,
        "_process_strategy_node",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        fts.time,
        "sleep",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("should not need to poll - there was never any work")
        ),
    )

    outstanding = SharedCounter(0)
    queue = _FakeQueue([])

    fts.optimize(queue, 0, [1], "9999", "tag", {}, outstanding)
