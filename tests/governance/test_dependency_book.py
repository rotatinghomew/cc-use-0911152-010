from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from export_portfolio.governance.decision_ledger import Repository
from export_portfolio.intake.initiative import ContractError, DomainRecord
from export_portfolio.network.dependency_risks import (
    STATE_EVIDENCED, STATE_FAILED, STATE_MISSING,
)
from export_portfolio.portfolio_office import Application
from export_portfolio.stages.investment_gates import decide, dependency_gate_blockers


class DependencyBookBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "dependencies.sqlite3"
        self.application = Application(self.database)
        self.book = self.application.dependency_book
        for project_id in ("p1", "p2", "p3", "p4"):
            self.application.repository.save_record(DomainRecord(project_id, {"target_country": project_id}))
        # n1 认证：p1 直接依赖，p2 经 p1 传导，p3 共同依赖；n2 伙伴：仅 p3 依赖；
        # n3 与 p4 构成与 n1 无关的独立子图
        self.book.register_node("n1", "certification", "东盟准入认证")
        self.book.register_node("n2", "partner", "本地伙伴承诺")
        self.book.register_node("n3", "resource", "共享交付资源")
        self.book.connect_project("p1", "n1")
        self.book.connect_project("p2", "p1", kind="upstream")
        self.book.connect_project("p3", "n1", exposure=0.7)
        self.book.connect_project("p3", "n2", exposure=0.4)
        self.book.connect_project("p4", "n3")


class DependencyBookLifecycleTests(DependencyBookBase):
    def test_new_nodes_start_missing_evidence(self):
        self.assertEqual(self.book.view().nodes["n1"], STATE_MISSING)
        report = self.book.analyze()
        self.assertEqual(set(report.blocked_projects), {"p1", "p2", "p3", "p4"})

    def test_bind_withdraw_and_rebind_evidence_keeps_revision_history(self):
        revision = self.book.bind_evidence("n1", {"document": "cert.pdf"})
        self.assertEqual(revision, 1)
        self.assertEqual(self.book.view().nodes["n1"], STATE_EVIDENCED)
        # 同证据幂等
        self.assertEqual(self.book.bind_evidence("n1", {"document": "cert.pdf"}), 1)
        self.book.withdraw_evidence("n1", "监管暂停")
        self.assertEqual(self.book.view().nodes["n1"], STATE_FAILED)
        new_revision = self.book.bind_evidence("n1", {"document": "cert-v2.pdf"})
        self.assertEqual(new_revision, 3)
        with self.application.repository.connect() as connection:
            statuses = [row["status"] for row in connection.execute(
                "SELECT status FROM dependency_evidence WHERE node_id='n1' ORDER BY revision")]
        # 修订 1 的原始证据保持 bound，修订 2 为撤回记录，修订 3 为重新绑定
        self.assertEqual(statuses, ["bound", "withdrawn", "bound"])

    def test_withdraw_without_evidence_rejected(self):
        with self.assertRaisesRegex(ContractError, "没有可撤回的证据"):
            self.book.withdraw_evidence("n2", "原因")

    def test_edges_require_known_projects_and_sources(self):
        with self.assertRaisesRegex(ContractError, "unknown project"):
            self.book.connect_project("ghost", "n1")
        with self.assertRaisesRegex(ContractError, "依赖来源必须是已登记节点或项目"):
            self.book.connect_project("p1", "ghost-node")

    def test_unknown_node_and_invalid_kind_rejected(self):
        with self.assertRaisesRegex(ContractError, "unknown dependency node"):
            self.book.analyze() and self.book.withdraw_evidence("ghost", "x")
        with self.assertRaisesRegex(ContractError, "依赖节点类型无效"):
            self.book.register_node("bad", "unclear", "说明")


class GateReviewTests(DependencyBookBase):
    def test_reviews_are_immutable_and_sequenced(self):
        first = self.book.record_gate_review("review-1", "board-review")
        self.assertEqual(first["sequence_no"], 1)
        with self.assertRaisesRegex(ContractError, "历史审议不可覆盖"):
            self.book.record_gate_review("review-1", "again")
        second = self.book.record_gate_review("review-2", "board-review")
        self.assertEqual(second["sequence_no"], 2)
        self.assertEqual(second["previous_review_id"], "review-1")

    def test_first_review_delta_lists_everything_as_new(self):
        review = self.book.record_gate_review("review-1", "board-review")
        self.assertTrue(review["delta"]["changed"])
        self.assertIsNone(review["delta"]["previous_fingerprint"])
        self.assertEqual(set(review["delta"]["newly_blocked"]), {"p1", "p2", "p3", "p4"})

    def test_blocker_rows_carry_evidence_coordinates(self):
        self.book.bind_evidence("n1", {"document": "cert.pdf"})
        review = self.book.record_gate_review("review-1", "board-review")
        blockers = {(row["project_id"], row["node_id"]): row for row in review["blockers"]}
        self.assertNotIn(("p1", "n1"), blockers)  # n1 已证，不再阻塞
        p3_n2 = blockers[("p3", "n2")]
        self.assertEqual(p3_n2["origin"], "missing_evidence")
        self.assertEqual(p3_n2["evidence_revision"], 0)
        # 证据台账只附在实际阻塞原因上；已恢复的 n1 不在阻塞证据链中
        trail = {item["node_id"]: item["revisions"] for item in review["evidence_trail"]}
        self.assertNotIn("n1", trail)
        self.assertEqual(trail["n2"], [])
        # 阻塞原因可追到完整传导路径
        report = review["report"]
        p3 = next(risk for risk in report["project_risks"] if risk["project_id"] == "p3")
        self.assertEqual({blocker["node_id"] for blocker in p3["blockers"]}, {"n2"})
        self.assertEqual(p3["blockers"][0]["paths"], [["n2", "p3"]])


class ReevaluationScopeTests(DependencyBookBase):
    def test_evidence_change_reevaluates_only_affected_projects(self):
        self.book.record_gate_review("review-1", "board-review")
        review = self.application.reevaluate_dependency_evidence(
            "n1", "review-2", evidence={"document": "cert.pdf"})
        # n1 影响 p1/p2/p3；独立子图 p4 不进入本次审议
        self.assertEqual(set(review["reevaluated_projects"]), {"p1", "p2", "p3"})
        self.assertEqual(review["unchanged_projects"], ["p4"])
        # 审议视图只含受影响子图：p3 仍被 n2 缺证拦住，p4 完全不在范围内
        self.assertEqual(review["report"]["blocked_projects"], ["p3"])
        # delta 按同口径（p1/p2/p3）比较，未重评的 p4 不会被误报为解除
        self.assertEqual(set(review["delta"]["cleared"]), {"p1", "p2"})
        self.assertEqual(review["delta"]["still_blocked"], ["p3"])
        self.assertNotIn("p4", review["delta"]["cleared"])

    def test_clearing_one_blocker_does_not_clear_another(self):
        self.book.bind_evidence("n1", {"document": "cert.pdf"})
        self.book.bind_evidence("n2", {"document": "partner-deed.pdf"})
        review = self.book.record_gate_review("review-1", "board-review")
        self.assertNotIn("p3", review["report"]["blocked_projects"])
        # 撤回 n1：p3 因 n1 重新阻塞，但 n2 的证据仍然有效
        changed = self.application.reevaluate_dependency_evidence(
            "n1", "review-2", withdraw_reason="认证暂停")
        p3 = next(risk for risk in changed["report"]["project_risks"] if risk["project_id"] == "p3")
        self.assertEqual([blocker["node_id"] for blocker in p3["blockers"]], ["n1"])
        self.assertEqual(p3["blockers"][0]["state"], STATE_FAILED)

    def test_withdraw_reintroduces_blockers_and_explains_delta(self):
        self.book.bind_evidence("n1", {"document": "cert.pdf"})
        self.book.record_gate_review("review-1", "board-review")
        changed = self.application.reevaluate_dependency_evidence(
            "n1", "review-2", withdraw_reason="认证暂停")
        # 受限范围为 p1/p2/p3：p1、p2 重新阻塞，p3 原本就被 n2 拦住
        self.assertEqual(set(changed["delta"]["newly_blocked"]), {"p1", "p2"})
        added = {tuple(pair) for pair in changed["delta"]["blockers_added"]}
        self.assertIn(("p2", "n1"), added)
        self.assertIn(("p1", "n1"), added)
        # 敞口增量只统计受影响子图：p1 与 p2 新增（各 1.0）；
        # p3 原本就被 n2 以项目级敞口 0.7 计入，撤回不改变其项目敞口，增量 0
        self.assertAlmostEqual(changed["delta"]["portfolio_exposure_delta"], 2.0)
        self.assertAlmostEqual(changed["report"]["portfolio_exposure"], 2.7)

    def test_scenario_is_comparison_only(self):
        baseline = self.book.analyze()
        simulated = self.application.simulate_dependency_scenario(
            {"n1": STATE_EVIDENCED, "n2": STATE_EVIDENCED, "n3": STATE_EVIDENCED})
        self.assertEqual(simulated["blocked_projects"], [])
        # 推演不改动台账，也不产生审议记录
        self.assertEqual(self.book.view().nodes["n1"], STATE_MISSING)
        self.assertEqual(self.book.list_gate_reviews(), [])
        self.assertEqual(self.book.analyze().fingerprint(), baseline.fingerprint())


class BlockerTraceTests(DependencyBookBase):
    def test_trace_follows_reviews_back_to_evidence(self):
        self.book.record_gate_review("review-1", "board-review")
        self.book.bind_evidence("n1", {"document": "cert.pdf"})
        self.book.record_gate_review("review-2", "board-review")
        self.book.withdraw_evidence("n1", "认证暂停")
        self.book.record_gate_review("review-3", "board-review")
        trace = self.application.trace_dependency_blocker("p2", "n1")
        self.assertEqual(trace["current_state"], STATE_FAILED)
        self.assertEqual([row["review_id"] for row in trace["review_history"]],
                         ["review-1", "review-3"])
        self.assertEqual([row["revision"] for row in trace["evidence"]], [1, 2])
        origins = {row["origin"] for row in trace["review_history"]}
        self.assertEqual(origins, {"missing_evidence", "propagated"})

    def test_trace_unknown_node_rejected(self):
        with self.assertRaisesRegex(ContractError, "unknown dependency node"):
            self.application.trace_dependency_blocker("p1", "ghost")


class GateIntegrationTests(DependencyBookBase):
    def test_dependency_blocker_forces_hold_even_when_record_advances(self):
        # n1 缺证：p1 即使准备度达标也必须 hold，不得推进
        record = DomainRecord("p1", {"target_country": "thailand"})
        advanced, decision = self.application.process(record)
        self.assertEqual(decision.outcome, "hold")
        self.assertTrue(any(reason.startswith("dependency:n1") for reason in decision.reasons))
        self.assertEqual(advanced.state, "holding")

    def test_unrelated_project_is_not_reprocessed_by_evidence_review(self):
        self.book.bind_evidence("n3", {"document": "capacity-deed.pdf"})
        review = self.application.reevaluate_dependency_evidence(
            "n3", "review-1", evidence={"document": "capacity-deed.pdf"})
        self.assertEqual(review["reevaluated_projects"], ["p4"])
        scope = json.loads(review["scope_projects_json"])
        self.assertEqual(scope, ["p4"])


class StageGateContractTests(DependencyBookBase):
    def test_decision_without_dependency_report_keeps_legacy_behavior(self):
        record = DomainRecord("p1", {"target_country": "thailand"})
        decision = decide(record, {})
        self.assertNotIn("dependency", " ".join(decision.reasons))
        self.assertEqual(dependency_gate_blockers(record, {}), [])

    def test_decision_holds_on_blocked_report_and_releases_when_cleared(self):
        record = DomainRecord("p1", {"target_country": "thailand"})
        blocked_report = self.book.analyze().as_dict()
        blocked = decide(record, {"dependency_report": blocked_report})
        self.assertEqual(blocked.outcome, "hold")
        self.assertTrue(any("dependency:n1:missing_evidence" in reason for reason in blocked.reasons))
        # 解除 n1 后（p1 不再被任何节点阻塞），同一项目回到无依赖拦截的正常门禁判定
        self.book.bind_evidence("n1", {"document": "cert.pdf"})
        cleared_report = self.book.analyze().as_dict()
        self.assertEqual(dependency_gate_blockers(record, {"dependency_report": cleared_report}), [])
        released = decide(record, {"dependency_report": cleared_report})
        self.assertNotIn("dependency", " ".join(released.reasons))

    def test_independent_blockers_survive_partial_clearance(self):
        record = DomainRecord("p3", {"target_country": "thailand"})
        self.book.bind_evidence("n1", {"document": "cert.pdf"})
        report = self.book.analyze().as_dict()
        blockers = dependency_gate_blockers(record, {"dependency_report": report})
        self.assertEqual([item["node_id"] for item in blockers], ["n2"])
        self.assertEqual(decide(record, {"dependency_report": report}).outcome, "hold")


if __name__ == "__main__":
    unittest.main()
