from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from export_portfolio.intake.initiative import ContractError, Decision, DomainRecord, records_from_fixture, stable_hash
from export_portfolio.governance.decision_ledger import Repository
from export_portfolio.stages.investment_gates import advance_for_outcome, decide
from export_portfolio.board.portfolio_book import export_bundle, verify_bundle
from export_portfolio.optimization.constrained_selection import derive_context

from export_portfolio.governance.resource_book import ResourceBook
from export_portfolio.governance.dependency_book import DependencyBook

class Application:
    def __init__(self, database: str | Path, context: Mapping[str, Any] | None = None):
        self.repository = Repository(database)
        self.resource_book = ResourceBook(self.repository)
        self.dependency_book = DependencyBook(self.repository)
        self.context = dict(context or {})

    def ingest(self, record: DomainRecord) -> DomainRecord:
        current = self.repository.get_record(record.record_id)
        if current is None:
            self.repository.save_record(record)
            return record
        if current.fingerprint == record.fingerprint:
            return current
        replacement = DomainRecord(record.record_id, record.payload, current.state,
                                   current.version + 1, record.occurred_at, record.source)
        self.repository.save_record(replacement, expected_version=current.version)
        return replacement

    def process(self, record: DomainRecord) -> tuple[DomainRecord, Decision]:
        stored = self.ingest(record)
        context = dict(self.context)
        context.update(derive_context(stored, self.repository))
        # 依赖网络传导报告进入阶段门禁：全网络分析（保留多路径与循环），按项目取阻塞；
        # 无登记依赖时报告为空，保持原有行为
        context['dependency_report'] = self.dependency_book.analyze().as_dict()
        decision = decide(stored, context)
        self.repository.save_decision(decision)
        advanced = advance_for_outcome(stored, decision)
        if advanced.fingerprint != stored.fingerprint:
            self.repository.save_record(advanced, expected_version=stored.version)
        return advanced, decision

    def run_fixture(self, fixture: Mapping[str, Any], output: str | Path) -> dict[str, Any]:
        records = records_from_fixture(fixture)
        fixture_hash = stable_hash(fixture)
        run_id = self.repository.begin_run(fixture_hash, len(records))
        completed = self.repository.completed_run(run_id)
        if completed is not None:
            return completed
        decisions: dict[str, Decision] = {}
        final_records: list[DomainRecord] = []
        for record in records:
            final_record, decision = self.process(record)
            final_records.append(final_record)
            decisions[record.record_id] = decision
        snapshot = self.repository.snapshot()
        if snapshot["chain_errors"]:
            raise ContractError("audit chain verification failed")
        manifest = export_bundle(output, final_records, decisions, snapshot, self.repository.event_rows())
        result = {"run_id": run_id, "fixture_hash": fixture_hash, "records": len(final_records),
                  "decisions": len(decisions), "manifest": manifest, "snapshot": snapshot}
        self.repository.finish_run(run_id, result)
        return result

    def inspect(self) -> dict[str, Any]:
        return {"snapshot": self.repository.snapshot(), "records": [record.__dict__ for record in self.repository.list_records()],
                "decisions": self.repository.decision_rows()}

    def policy_impact(self, event_id: str) -> dict[str, Any]:
        return self.repository.policy_impact(event_id)

    def board_dependency_review(self, review_id: str, trigger: str = "board-review") -> dict[str, Any]:
        """董事会例行审议：沉淀全量依赖网络传导快照及相对上次的实质变化。"""
        return self.dependency_book.record_gate_review(review_id, trigger)

    def reevaluate_dependency_evidence(self, node_id: str, review_id: str,
                                       evidence: Mapping[str, Any] | None = None,
                                       withdraw_reason: str | None = None,
                                       mark_missing: bool = False) -> dict[str, Any]:
        """真实证据变化后只重评受影响的待推进项目，其他项目不进入本次审议。"""
        return self.dependency_book.reevaluate_after_evidence_change(
            node_id, review_id, evidence=evidence,
            withdraw_reason=withdraw_reason, mark_missing=mark_missing)

    def simulate_dependency_scenario(self, overrides: Mapping[str, str],
                                     project_ids: list[str] | None = None) -> dict[str, Any]:
        """情景推演：仅作比较，不落库、不改项目实际阶段。"""
        return self.dependency_book.run_scenario(overrides, project_ids).as_dict()

    def trace_dependency_blocker(self, project_id: str, node_id: str) -> dict[str, Any]:
        """沿阻塞原因追到历次审议与所用证据修订。"""
        return self.dependency_book.trace_blocker(project_id, node_id)


def load_fixture(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read fixture: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("fixture root must be an object")
    return value


def run(fixture_path: str | Path, database: str | Path, output: str | Path,
        context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return Application(database, context).run_fixture(load_fixture(fixture_path), output)


def verify(database: str | Path, output: str | Path) -> dict[str, Any]:
    repository = Repository(database)
    chain_errors = repository.verify_chain()
    bundle_errors = verify_bundle(output)
    return {"ok": not chain_errors and not bundle_errors, "chain_errors": chain_errors,
            "bundle_errors": bundle_errors, "snapshot": repository.snapshot()}
