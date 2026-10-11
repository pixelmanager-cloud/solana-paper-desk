"""Reproduce the pre-T22 failure semantics for tests of the legacy per-incident receipt machinery.

Before T22 a charged pass that did not finish kept a NULL outcome and an interrupted dispatch stayed
unresolved; the reviewed receipt kinds (terminal reconciliation, HTTP 403/oversize/dispatch retirements,
empty-history and migration dispositions) exist to certify exactly those retained states, and old store sets
still carry them. Those tests need a faithful pre-T22 state, so they disable the generic closure machinery
(`paper_pass_closure` writers and the abandoned-pass recovery) for their duration. Tests of the closure
machinery itself must NOT call this.
"""
from unittest.mock import patch


def install(case):
    from desk import paper_cycle
    from tools import paper_entry_dispatcher
    for target, name in ((paper_cycle, '_close_failed'), (paper_cycle, '_close_unfinished'),
                         (paper_entry_dispatcher, '_close_dispatch')):
        patcher = patch.object(target, name, return_value=None)
        patcher.start()
        case.addCleanup(patcher.stop)
    patcher = patch('desk.paper_pass_closure.recover_abandoned',
                    return_value={'closed': [], 'refused': [], 'monitoring_resolved': []})
    patcher.start()
    case.addCleanup(patcher.stop)
