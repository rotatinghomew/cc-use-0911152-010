from __future__ import annotations

import unittest

from export_portfolio.intake.initiative import ContractError
from export_portfolio.network.dependency_risks import (
    EDGE_REQUIRES, EDGE_UPSTREAM, ORIGIN_DIRECT, ORIGIN_MISSING, ORIGIN_PROPAGATED,
    STATE_EVIDENCED, STATE_FAILED, STATE_MISSING, STATE_STALE, DependencyView,
    find_cycles, propagate, report_from_dict, review_delta, simulate, validate_state,
)


def build_view() -> DependencyView:
    # n1 失效：p1 直接依赖；p2 通过 p1 被传导；p3 与 p1 共同依赖 n1（共同失效）
    # n2 缺证：p3 直接依赖；n3 已证：p4 依赖但不受阻
    return DependencyView(
        nodes={"n1": STATE_FAILED, "n2": STATE_MISSING, "n3": STATE_EVIDENCED, "n4": STATE_STALE},
        node_revisions={"n1": 2, "n2": 0, "n3": 1, "n4": 3},
        edges=[
            ("n1", "p1", EDGE_REQUIRES, "node"),
            ("p1", "p2", EDGE_UPSTREAM, "project"),
            ("n1", "p3", EDGE_REQUIRES, "node"),
            ("n2", "p3", EDGE_REQUIRES, "node"),
            ("n3", "p4", EDGE_REQUIRES, "node"),
            ("n4", "p4", EDGE_REQUIRES, "node"),
        ],
        projects=["p1", "p2", "p3", "p4"],
        exposures={"p1": 0.5, "p2": 1.0, "p3": 0.8, "p4": 0.25},
    )


class PropagationClassificationTests(unittest.TestCase):
    def test_direct_propagated_and_missing_are_distinct(self):
        report = propagate(build_view())
        risks = {risk.project_id: risk for risk in report.project_risks}
        self.assertEqual(risks["p1"].blockers[0].origin, ORIGIN_DIRECT)
        self.assertEqual(risks["p2"].blockers[0].origin, ORIGIN_PROPAGATED)
        origins_p3 = {blocker.origin for blocker in risks["p3"].blockers}
        self.assertEqual(origins_p3, {ORIGIN_DIRECT, ORIGIN_MISSING})
        self.assertEqual(risks["p4"].blockers[0].state, STATE_STALE)
        self.assertTrue(risks["p4"].blocked)

    def test_paths_explain_multi_hop_transmission(self):
        report = propagate(build_view())
        p2 = next(risk for risk in report.project_risks if risk.project_id == "p2")
        self.assertEqual(p2.blockers[0].paths, (("n1", "p1", "p2"),))
        p1 = next(risk for risk in report.project_risks if risk.project_id == "p1")
        self.assertEqual(p1.blockers[0].paths, (("n1", "p1"),))

    def test_shared_failure_is_reported_once_per_node(self):
        report = propagate(build_view())
        n1 = next(item for item in report.node_failures if item.node_id == "n1")
        self.assertTrue(n1.shared)
        self.assertEqual(n1.direct_projects, ("p1", "p3"))
        self.assertEqual(n1.propagated_projects, ("p2",))
        # 节点损失只在 node_failures 里出现一次
        self.assertEqual([item.node_id for item in report.node_failures].count("n1"), 1)

    def test_portfolio_exposure_does_not_double_count_paths(self):
        report = propagate(build_view())
        # p1 0.5 + p2 1.0 + p3 0.8（两条阻塞只计一次）+ p4 0.25
        self.assertAlmostEqual(report.portfolio_exposure, 2.55)
        p3 = next(risk for risk in report.project_risks if risk.project_id == "p3")
        self.assertEqual(p3.exposure, 0.8)

    def test_origin_counts_cover_each_blocker_once(self):
        report = propagate(build_view())
        total = sum(report.blocked_origin_counts.values())
        blockers = sum(len(risk.blockers) for risk in report.project_risks)
        self.assertEqual(total, blockers)
        self.assertEqual(report.blocked_origin_counts[ORIGIN_MISSING], 1)
        self.assertEqual(report.blocked_origin_counts[ORIGIN_PROPAGATED], 1)

    def test_evidenced_node_never_blocks(self):
        view = DependencyView(nodes={"ok": STATE_EVIDENCED}, node_revisions={"ok": 1},
                              edges=[("ok", "p1", EDGE_REQUIRES, "node")],
                              projects=["p1"], exposures={})
        report = propagate(view)
        self.assertEqual(report.blocked_projects, ())
        self.assertEqual(report.node_failures, ())

    def test_multiple_paths_to_same_project_keep_one_blocker(self):
        # n1 -> p1 -> p3 与 n1 -> p2 -> p3 两条传导路径到达同一项目
        view = DependencyView(
            nodes={"n1": STATE_FAILED}, node_revisions={"n1": 1},
            edges=[("n1", "p1", EDGE_REQUIRES, "node"),
                   ("n1", "p2", EDGE_REQUIRES, "node"),
                   ("p1", "p3", EDGE_UPSTREAM, "project"),
                   ("p2", "p3", EDGE_UPSTREAM, "project")],
            projects=["p1", "p2", "p3"], exposures={})
        report = propagate(view)
        p3 = next(risk for risk in report.project_risks if risk.project_id == "p3")
        self.assertEqual(len(p3.blockers), 1)
        # 但两条路径都保留下来供审议解释
        self.assertEqual(len(p3.blockers[0].paths), 2)
        self.assertEqual(report.portfolio_exposure, 3.0)

    def test_stale_evidence_is_blocking_but_distinct_from_failure(self):
        view = DependencyView(nodes={"n1": STATE_STALE}, node_revisions={"n1": 3},
                              edges=[("n1", "p1", EDGE_REQUIRES, "node")],
                              projects=["p1"], exposures={})
        report = propagate(view)
        self.assertEqual(report.blocked_projects, ("p1",))
        self.assertEqual(report.project_risks[0].blockers[0].state, STATE_STALE)
        self.assertEqual(report.project_risks[0].blockers[0].origin, ORIGIN_DIRECT)


class CycleTests(unittest.TestCase):
    def test_self_loop_is_detected(self):
        view = DependencyView(
            nodes={"n1": STATE_FAILED}, node_revisions={"n1": 1},
            edges=[("n1", "p1", EDGE_REQUIRES, "node"),
                   ("p1", "p1", EDGE_UPSTREAM, "project")],
            projects=["p1"], exposures={})
        report = propagate(view)
        self.assertEqual(report.cycles, (("p1",),))
        risk = report.project_risks[0]
        self.assertEqual(risk.cycle_refs, (("p1",),))

    def test_multi_project_cycle_keeps_propagating(self):
        view = DependencyView(
            nodes={"n1": STATE_FAILED}, node_revisions={"n1": 1},
            edges=[("n1", "a", EDGE_REQUIRES, "node"),
                   ("a", "b", EDGE_UPSTREAM, "project"),
                   ("b", "a", EDGE_UPSTREAM, "project")],
            projects=["a", "b"], exposures={})
        report = propagate(view)
        self.assertEqual(report.cycles, (("a", "b"),))
        self.assertEqual(report.blocked_projects, ("a", "b"))
        b = next(risk for risk in report.project_risks if risk.project_id == "b")
        self.assertEqual(b.blockers[0].origin, ORIGIN_PROPAGATED)

    def test_no_cycle_when_acyclic(self):
        self.assertEqual(find_cycles(build_view()), ())


class ScenarioAndDeltaTests(unittest.TestCase):
    def test_scenario_clears_only_target_node(self):
        view = build_view()
        baseline = propagate(view)
        simulated = simulate(view, {"n1": STATE_EVIDENCED})
        # n1 恢复解除 p1/p2，但 p3 仍被 n2 缺证拦住、p4 仍被 n4 过时拦住
        self.assertEqual(simulated.blocked_projects, ("p3", "p4"))
        delta = review_delta(baseline, simulated)
        self.assertEqual(delta["cleared"], ["p1", "p2"])
        self.assertEqual(delta["still_blocked"], ["p3", "p4"])
        removed_pairs = {tuple(pair) for pair in delta["blockers_removed"]}
        self.assertIn(("p3", "n1"), removed_pairs)
        self.assertNotIn(("p3", "n2"), removed_pairs)
        self.assertTrue(delta["changed"])

    def test_scenario_does_not_mutate_source_view(self):
        view = build_view()
        simulate(view, {"n1": STATE_EVIDENCED, "n2": STATE_EVIDENCED})
        self.assertEqual(view.nodes["n1"], STATE_FAILED)
        self.assertEqual(view.nodes["n2"], STATE_MISSING)

    def test_scenario_rejects_unknown_node(self):
        with self.assertRaisesRegex(ContractError, "unknown dependency node"):
            simulate(build_view(), {"ghost": STATE_FAILED})

    def test_delta_reports_no_change_on_identical_reviews(self):
        report = propagate(build_view())
        delta = review_delta(report, propagate(build_view()))
        self.assertFalse(delta["changed"])
        self.assertEqual(delta["newly_blocked"], [])
        self.assertEqual(delta["cleared"], [])

    def test_first_review_delta_has_no_previous(self):
        report = propagate(build_view())
        delta = review_delta(None, report)
        self.assertTrue(delta["changed"])
        self.assertIsNone(delta["previous_fingerprint"])
        self.assertEqual(delta["newly_blocked"], list(report.blocked_projects))

    def test_report_round_trips_through_dict(self):
        report = propagate(build_view())
        rebuilt = report_from_dict(report.as_dict())
        self.assertEqual(rebuilt.fingerprint(), report.fingerprint())
        self.assertEqual(rebuilt.blocked_projects, report.blocked_projects)
        self.assertEqual(rebuilt.cycles, report.cycles)

    def test_invalid_state_rejected(self):
        with self.assertRaises(ContractError):
            validate_state("dreamed")


if __name__ == "__main__":
    unittest.main()
