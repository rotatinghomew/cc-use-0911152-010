"""依赖网络台账：登记共享依赖节点、项目依赖边与真实证据，并沉淀历次门禁审议。

设计约束（对应董事会诉求）：
- 证据按身份与修订号保存，缺证（从未绑定）与失效/过时（绑定后撤回或修订落后）分开；
- 解除一个节点的阻塞只会重评受其影响的待推进项目，不会误解除其他节点的阻塞；
- 门禁审议不可改写，每次审议沿阻塞原因可追溯到所用证据修订与完整传导路径；
- 情景推演只调用只读视图，不写入本台账，也不改动项目实际阶段。
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from export_portfolio.intake.initiative import ContractError, canonical_json, identifier, stable_hash, utc_now
from export_portfolio.network.dependency_risks import (
    EDGE_REQUIRES, EDGE_UPSTREAM, STATE_EVIDENCED, STATE_FAILED,
    STATE_MISSING, DependencyView, PropagationReport, filter_report, propagate,
    report_from_dict, review_delta, simulate, validate_edge_kind,
)

NODE_KINDS = ("partner", "certification", "resource", "other")


def prepare_dependencies(connection) -> None:
    connection.executescript("""CREATE TABLE IF NOT EXISTS dependency_nodes(
        node_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        description TEXT NOT NULL,
        effective_state TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS dependency_edges(
        source_id TEXT NOT NULL,
        target_project TEXT NOT NULL,
        edge_kind TEXT NOT NULL,
        source_type TEXT NOT NULL CHECK(source_type IN ('node','project')),
        exposure REAL NOT NULL DEFAULT 1.0,
        registered_at TEXT NOT NULL,
        PRIMARY KEY(source_id,target_project,edge_kind,source_type)
    );
    CREATE TABLE IF NOT EXISTS dependency_evidence(
        node_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('bound','withdrawn')),
        evidence_json TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        bound_at TEXT NOT NULL,
        PRIMARY KEY(node_id,revision),
        FOREIGN KEY(node_id) REFERENCES dependency_nodes(node_id)
    );
    CREATE TABLE IF NOT EXISTS gate_reviews(
        review_id TEXT PRIMARY KEY,
        sequence_no INTEGER NOT NULL,
        trigger TEXT NOT NULL,
        scope_projects_json TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        report_hash TEXT NOT NULL,
        report_json TEXT NOT NULL,
        delta_json TEXT NOT NULL,
        previous_review_id TEXT,
        opened_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gate_review_blockers(
        review_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        state TEXT NOT NULL,
        evidence_revision INTEGER NOT NULL,
        blocker_hash TEXT NOT NULL,
        PRIMARY KEY(review_id,project_id,node_id),
        FOREIGN KEY(review_id) REFERENCES gate_reviews(review_id)
    );
    CREATE INDEX IF NOT EXISTS idx_gate_reviews_opened ON gate_reviews(opened_at);
    CREATE INDEX IF NOT EXISTS idx_gate_blockers_node ON gate_review_blockers(node_id,review_id);
    """)


class DependencyBook:
    def __init__(self, repository):
        self.repository = repository
        with repository.connect() as connection:
            prepare_dependencies(connection)

    # ------------------------------------------------------------------ 登记
    def register_node(self, node_id: str, kind: str, description: str) -> str:
        node_id = identifier(node_id)
        if kind not in NODE_KINDS:
            raise ContractError("依赖节点类型无效")
        if not isinstance(description, str) or not description.strip():
            raise ContractError("依赖节点需要说明")
        with self.repository.transaction() as connection:
            old = connection.execute("SELECT * FROM dependency_nodes WHERE node_id=?", (node_id,)).fetchone()
            if old:
                if old["kind"] != kind:
                    raise ContractError("依赖节点类型不可改写")
                return node_id
            connection.execute(
                "INSERT INTO dependency_nodes VALUES(?,?,?,?,?,?)",
                (node_id, kind, description.strip(), STATE_MISSING, 0, utc_now()))
            return node_id

    def bind_evidence(self, node_id: str, evidence: Mapping[str, Any]) -> int:
        """绑定真实证据：缺证节点转为已证；已证节点内容变化产生新修订。"""
        node_id = identifier(node_id)
        if not isinstance(evidence, Mapping) or not evidence:
            raise ContractError("依赖证据必须是非空对象")
        body = canonical_json(dict(evidence))
        evidence_hash = stable_hash(dict(evidence))
        with self.repository.transaction() as connection:
            node = self._require_node(connection, node_id)
            latest_revision = node["revision"]
            if latest_revision:
                latest = connection.execute(
                    "SELECT * FROM dependency_evidence WHERE node_id=? AND revision=?",
                    (node_id, latest_revision)).fetchone()
                if latest["status"] == "bound" and latest["evidence_hash"] == evidence_hash:
                    return latest_revision  # 同一证据幂等重放
            revision = latest_revision + 1
            connection.execute("INSERT INTO dependency_evidence VALUES(?,?,?,?,?,?)",
                               (node_id, revision, "bound", body, evidence_hash, utc_now()))
            connection.execute(
                "UPDATE dependency_nodes SET effective_state=?,revision=?,updated_at=? WHERE node_id=?",
                (STATE_EVIDENCED, revision, utc_now(), node_id))
            return revision

    def withdraw_evidence(self, node_id: str, reason: str) -> None:
        """撤回当前有效证据：节点转为失效，旧证据保留可追溯。"""
        node_id = identifier(node_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError("撤回证据需要原因")
        with self.repository.transaction() as connection:
            node = self._require_node(connection, node_id)
            if not node["revision"]:
                raise ContractError("缺证节点没有可撤回的证据")
            current_revision = node["revision"]
            current = connection.execute(
                "SELECT status FROM dependency_evidence WHERE node_id=? AND revision=?",
                (node_id, current_revision)).fetchone()
            if current is None or current["status"] != "bound":
                raise ContractError("当前修订不是有效证据，无法撤回")
            note = canonical_json({"withdrawn_reason": reason.strip(),
                                   "withdrawn_revision": current_revision})
            connection.execute(
                "INSERT INTO dependency_evidence VALUES(?,?,?,?,?,?)",
                (node_id, current_revision + 1, "withdrawn", note,
                 stable_hash({"node_id": node_id, "note": note}), utc_now()))
            connection.execute(
                "UPDATE dependency_nodes SET effective_state=?,revision=?,updated_at=? WHERE node_id=?",
                (STATE_FAILED, current_revision + 1, utc_now(), node_id))

    def mark_missing(self, node_id: str) -> None:
        """显式把节点标记为缺证（例如尽调确认承诺尚未落实）。"""
        node_id = identifier(node_id)
        with self.repository.transaction() as connection:
            node = self._require_node(connection, node_id)
            if node["effective_state"] != STATE_MISSING:
                connection.execute(
                    "UPDATE dependency_nodes SET updated_at=? WHERE node_id=?", (utc_now(), node_id))

    def connect_project(self, project_id: str, source_id: str, kind: str = EDGE_REQUIRES,
                        exposure: float = 1.0) -> None:
        """登记依赖边：source 为节点时 project 直接依赖它；source 为项目时是项目间上游依赖。"""
        project_id, source_id = identifier(project_id), identifier(source_id)
        validate_edge_kind(kind)
        if type(exposure) not in (int, float) or not 0.0 <= float(exposure) <= 1.0:
            raise ContractError("依赖敞口必须在零到一之间")
        with self.repository.transaction() as connection:
            if not connection.execute("SELECT 1 FROM records WHERE record_id=?", (project_id,)).fetchone():
                raise ContractError("unknown project")
            node = connection.execute("SELECT 1 FROM dependency_nodes WHERE node_id=?", (source_id,)).fetchone()
            project = connection.execute("SELECT 1 FROM records WHERE record_id=?", (source_id,)).fetchone()
            if not node and not project:
                raise ContractError("依赖来源必须是已登记节点或项目")
            source_type = "node" if node else "project"
            if source_type == "project" and source_id == project_id:
                raise ContractError("项目自依赖必须显式登记为独立边")
            connection.execute(
                "INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?,?,?,?)",
                (source_id, project_id, kind, source_type, float(exposure), utc_now()))

    def connect_self_cycle(self, project_id: str, kind: str = EDGE_UPSTREAM) -> None:
        """显式登记项目自环（循环依赖的一种）。"""
        project_id = identifier(project_id)
        validate_edge_kind(kind)
        with self.repository.transaction() as connection:
            if not connection.execute("SELECT 1 FROM records WHERE record_id=?", (project_id,)).fetchone():
                raise ContractError("unknown project")
            connection.execute(
                "INSERT OR IGNORE INTO dependency_edges VALUES(?,?,?,?,?,?)",
                (project_id, project_id, kind, "project", 1.0, utc_now()))

    # ------------------------------------------------------------------ 视图
    def _require_node(self, connection, node_id: str):
        node = connection.execute("SELECT * FROM dependency_nodes WHERE node_id=?", (node_id,)).fetchone()
        if node is None:
            raise ContractError("unknown dependency node")
        return node

    def _effective_state(self, connection, node) -> str:
        """结合证据台账计算有效状态：结论前被改过的证据视为过时阻塞。"""
        if node["effective_state"] == STATE_MISSING:
            return STATE_MISSING
        revision = node["revision"]
        latest = connection.execute(
            "SELECT status FROM dependency_evidence WHERE node_id=? AND revision=?",
            (node["node_id"], revision)).fetchone()
        if latest is None or latest["status"] != "bound":
            return STATE_FAILED
        # 存在更新的已绑定修订而节点未跟进时为过时（正常流程节点修订会同步，这里防御性处理）
        newer = connection.execute(
            "SELECT MAX(revision) AS rev FROM dependency_evidence WHERE node_id=?",
            (node["node_id"],)).fetchone()
        if newer["rev"] and newer["rev"] > revision:
            return STATE_STALE
        return node["effective_state"]

    def view(self, project_ids: Sequence[str] | None = None) -> DependencyView:
        with self.repository.connect() as connection:
            nodes = {row["node_id"]: self._effective_state(connection, row)
                     for row in connection.execute("SELECT * FROM dependency_nodes ORDER BY node_id")}
            revisions = {row["node_id"]: row["revision"]
                         for row in connection.execute("SELECT node_id,revision FROM dependency_nodes")}
            edge_rows = connection.execute(
                "SELECT * FROM dependency_edges ORDER BY source_id,target_project").fetchall()
            if project_ids is not None:
                # 闭包：从指定项目反向收集其全部上游（节点与项目），
                # 保证证据变化只重评受影响子图，不把无关项目带入审议
                wanted = {identifier(project) for project in project_ids}
                incoming: dict[str, list] = {}
                for row in edge_rows:
                    incoming.setdefault(row["target_project"], []).append(row)
                scope = set(wanted)
                pending = list(wanted)
                while pending:
                    current = pending.pop()
                    for row in incoming.get(current, ()):
                        if row["source_id"] not in scope:
                            scope.add(row["source_id"])
                            if row["source_type"] == "project":
                                pending.append(row["source_id"])
                edge_rows = [row for row in edge_rows
                             if row["target_project"] in scope and row["source_id"] in scope]
            projects = sorted({row["target_project"] for row in edge_rows}
                              | {row["source_id"] for row in edge_rows if row["source_type"] == "project"})
            # 同一项目可能依赖多个节点，项目级敞口取各依赖边的最大值（每项目只计一次）
            exposures: dict[str, float] = {}
            for row in edge_rows:
                if row["source_type"] == "node":
                    exposures[row["target_project"]] = max(
                        exposures.get(row["target_project"], 0.0), float(row["exposure"]))
            edges = [(row["source_id"], row["target_project"], row["edge_kind"], row["source_type"])
                     for row in edge_rows]
        return DependencyView(nodes=nodes, node_revisions=revisions, edges=edges,
                              projects=projects, exposures=exposures)

    def analyze(self, project_ids: Sequence[str] | None = None) -> PropagationReport:
        return propagate(self.view(project_ids))

    def affected_projects(self, node_id: str) -> list[str]:
        """沿依赖边（含项目间传导）找出受节点影响的全部项目。"""
        node_id = identifier(node_id)
        view = self.view()
        graph = view.adjacency()
        seen: set[str] = set()
        pending = [f"node:{node_id}"]
        while pending:
            current = pending.pop()
            for target, _kind, _source in graph.get(current, ()):
                if target.startswith("project:") and target not in seen:
                    seen.add(target)
                    pending.append(target)
        return sorted(item.split(":", 1)[1] for item in seen)

    def run_scenario(self, overrides: Mapping[str, str],
                     project_ids: Sequence[str] | None = None) -> PropagationReport:
        """情景推演：只读，不落库、不改阶段。"""
        return simulate(self.view(project_ids), overrides)

    # ------------------------------------------------------------------ 门禁审议
    def _input_hash(self, view: DependencyView) -> str:
        return stable_hash({"nodes": dict(sorted(view.nodes.items())),
                            "revisions": dict(sorted(view.node_revisions.items())),
                            "edges": sorted(view.edges),
                            "projects": sorted(view.projects),
                            "exposures": dict(sorted(view.exposures.items()))})

    def record_gate_review(self, review_id: str, trigger: str,
                           scope_projects: Sequence[str] | None = None,
                           only_affected_node: str | None = None) -> dict[str, Any]:
        """沉淀一次门禁审议；历史审议按 review_id 独立留存且不可覆盖。"""
        review_id = identifier(review_id)
        if not isinstance(trigger, str) or not trigger.strip():
            raise ContractError("门禁审议需要触发原因")
        with self.repository.transaction() as connection:
            prepare_dependencies(connection)
            existing = connection.execute("SELECT * FROM gate_reviews WHERE review_id=?", (review_id,)).fetchone()
            if existing:
                raise ContractError("门禁审议标识已存在，历史审议不可覆盖")
            view = self.view(scope_projects)
            report = propagate(view)
            previous_row = connection.execute(
                "SELECT * FROM gate_reviews ORDER BY sequence_no DESC LIMIT 1"
            ).fetchone()
            previous_report = report_from_dict(json.loads(previous_row["report_json"])) if previous_row else None
            if previous_report is not None and scope_projects is not None:
                # 受限重评：把上次全量报告裁剪到同一项目范围后再对比，
                # 未进入本次审议的项目不会被误报为“已解除”
                previous_report = filter_report(previous_report, view.projects)
            delta = review_delta(previous_report, report)
            sequence_no = (previous_row["sequence_no"] + 1) if previous_row else 1
            input_hash = self._input_hash(view)
            report_body = canonical_json(_report_payload(report))
            report_hash = report.fingerprint()
            connection.execute(
                "INSERT INTO gate_reviews VALUES(?,?,?,?,?,?,?,?,?,?)",
                (review_id, sequence_no, trigger.strip(),
                 canonical_json(sorted(view.projects)), input_hash, report_hash,
                 report_body, canonical_json(delta),
                 previous_row["review_id"] if previous_row else None, utc_now()))
            for risk in report.project_risks:
                for blocker in risk.blockers:
                    blocker_hash = stable_hash(
                        {"project": blocker.project_id, "node": blocker.node_id,
                         "state": blocker.state, "origin": blocker.origin,
                         "revision": blocker.evidence_revision, "paths": blocker.paths})
                    connection.execute(
                        "INSERT INTO gate_review_blockers VALUES(?,?,?,?,?,?,?)",
                        (review_id, blocker.project_id, blocker.node_id, blocker.origin,
                         blocker.state, blocker.evidence_revision, blocker_hash))
        return self.get_gate_review(review_id, only_affected_node=only_affected_node)

    def get_gate_review(self, review_id: str,
                        only_affected_node: str | None = None) -> dict[str, Any]:
        review_id = identifier(review_id)
        with self.repository.connect() as connection:
            row = connection.execute("SELECT * FROM gate_reviews WHERE review_id=?", (review_id,)).fetchone()
            if row is None:
                raise ContractError("unknown gate review")
            blockers = [dict(item) for item in connection.execute(
                "SELECT * FROM gate_review_blockers WHERE review_id=? ORDER BY project_id,node_id",
                (review_id,))]
            evidence = []
            for blocker in blockers:
                revision_rows = connection.execute(
                    "SELECT revision,status,evidence_hash,bound_at FROM dependency_evidence WHERE node_id=? ORDER BY revision",
                    (blocker["node_id"],)).fetchall()
                evidence.append({"node_id": blocker["node_id"],
                                 "revisions": [dict(item) for item in revision_rows]})
        result = dict(row) | {"blockers": blockers, "evidence_trail": evidence,
                              "report": json.loads(row["report_json"]),
                              "delta": json.loads(row["delta_json"])}
        if only_affected_node is not None:
            node_id = identifier(only_affected_node)
            result["affected_projects"] = self.affected_projects(node_id)
        return result

    def list_gate_reviews(self) -> list[dict[str, Any]]:
        with self.repository.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT review_id,sequence_no,trigger,input_hash,report_hash,previous_review_id,opened_at "
                "FROM gate_reviews ORDER BY sequence_no")]
        return rows

    def trace_blocker(self, project_id: str, node_id: str) -> dict[str, Any]:
        """沿阻塞原因追到历次审议与所用证据修订。"""
        project_id, node_id = identifier(project_id), identifier(node_id)
        with self.repository.connect() as connection:
            node = connection.execute("SELECT * FROM dependency_nodes WHERE node_id=?", (node_id,)).fetchone()
            if node is None:
                raise ContractError("unknown dependency node")
            review_rows = connection.execute(
                "SELECT g.review_id,g.sequence_no,g.trigger,g.opened_at,b.origin,b.state,b.evidence_revision,b.blocker_hash "
                "FROM gate_review_blockers b JOIN gate_reviews g ON g.review_id=b.review_id "
                "WHERE b.project_id=? AND b.node_id=? ORDER BY g.sequence_no",
                (project_id, node_id)).fetchall()
            evidence_rows = connection.execute(
                "SELECT revision,status,evidence_json,evidence_hash,bound_at FROM dependency_evidence "
                "WHERE node_id=? ORDER BY revision", (node_id,)).fetchall()
            edges = [dict(row) for row in connection.execute(
                "SELECT * FROM dependency_edges WHERE source_id=? OR target_project=? ORDER BY target_project",
                (node_id, project_id))]
        return {"project_id": project_id, "node_id": node_id,
                "current_state": self._node_state(node_id),
                "evidence": [dict(row) for row in evidence_rows],
                "review_history": [dict(row) for row in review_rows], "edges": edges}

    def _node_state(self, node_id: str) -> str:
        with self.repository.connect() as connection:
            return self._effective_state(connection, self._require_node(connection, node_id))

    # ------------------------------------------------- 证据变化 -> 受限重评
    def reevaluate_after_evidence_change(self, node_id: str, review_id: str,
                                         evidence: Mapping[str, Any] | None = None,
                                         withdraw_reason: str | None = None,
                                         mark_missing: bool = False) -> dict[str, Any]:
        """真实证据变化的唯一入口：更新节点后，只对受影响的待推进项目重评。

        - 受影响范围由依赖网络可达性决定，与该节点无关的项目不进入本次审议；
        - 解除本节点阻塞不会触碰其他节点造成的阻塞（阻塞按 project+node 独立存储）；
        - 返回的审议记录带相对上次的 delta，说明真正改变了什么。
        """
        node_id = identifier(node_id)
        if evidence is not None:
            self.bind_evidence(node_id, evidence)
        elif withdraw_reason is not None:
            self.withdraw_evidence(node_id, withdraw_reason)
        elif mark_missing:
            self.mark_missing(node_id)
        else:
            raise ContractError("证据变化必须提供新证据、撤回原因或缺证标记")
        affected = self.affected_projects(node_id)
        review = self.record_gate_review(
            review_id, trigger=f"evidence-change:{node_id}",
            scope_projects=affected, only_affected_node=node_id)
        review["reevaluated_projects"] = affected
        review["unchanged_projects"] = sorted(set(self.view().projects) - set(affected))
        return review


def _report_payload(report: PropagationReport) -> dict[str, Any]:
    return report.as_dict()
