"""依赖网络风险传导分析（纯函数，无持久化）。

把三类情形明确区分，避免把同一损失重复放大：
- direct：项目直接依赖一个当前不可用的依赖节点（直接依赖）；
- propagated：项目通过其他项目/节点的依赖链，被上游失效传导（传导依赖）；
- shared_failure：多个项目共同依赖同一不可用节点（共同失效），
  节点损失只在节点侧计一次，组合敞口按项目汇总且标注来源；
- missing_evidence：项目依赖的节点尚未绑定真实证据（缺证），
  与“证据存在但已失效”分开列示。

图中同时存在 node（伙伴/认证/其他共享依赖）与 project（出海项目）两类顶点，
依赖边 directed 为“被依赖方 -> 依赖方”，即失败沿边正向传导。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from export_portfolio.intake.initiative import ContractError, identifier, stable_hash

# 依赖节点的有效状态（effective state）
STATE_EVIDENCED = "evidenced"          # 已绑定真实证据且未撤回、未过时
STATE_FAILED = "failed"                # 证据存在但已撤回/失效
STATE_MISSING = "missing_evidence"     # 从未绑定真实证据（缺证）
STATE_STALE = "stale"                  # 证据修订落后于当前有效修订（结论前被改过）

BLOCKING_STATES = frozenset({STATE_FAILED, STATE_MISSING, STATE_STALE})

# 阻塞来源分类
ORIGIN_DIRECT = "direct"
ORIGIN_PROPAGATED = "propagated"
ORIGIN_SHARED = "shared_failure"
ORIGIN_MISSING = "missing_evidence"

# 边的语义
EDGE_REQUIRES = "requires"             # 项目显式需求
EDGE_UPSTREAM = "upstream"             # 项目间的上游交付依赖
EDGE_SHARED = "shared"                 # 共享资源/认证的间接关联


def _node_key(node_id: str) -> str:
    return "node:" + identifier(node_id)


def _project_key(project_id: str) -> str:
    return "project:" + identifier(project_id)


def _bare(key: str) -> str:
    return key.split(":", 1)[1]


def validate_state(state: str) -> str:
    if state not in {STATE_EVIDENCED, STATE_FAILED, STATE_MISSING, STATE_STALE}:
        raise ContractError(f"unknown dependency node state: {state}")
    return state


def validate_edge_kind(kind: str) -> str:
    if kind not in {EDGE_REQUIRES, EDGE_UPSTREAM, EDGE_SHARED}:
        raise ContractError(f"unknown dependency edge kind: {kind}")
    return kind


@dataclass(frozen=True)
class DependencyView:
    """门禁/分析所用的只读依赖网络快照。"""
    nodes: Mapping[str, str]                       # node_id -> effective state
    node_revisions: Mapping[str, int]              # node_id -> 当前证据修订
    edges: Sequence[tuple[str, str, str, str]]     # (source, target, kind, source_type)
    projects: Sequence[str]
    exposures: Mapping[str, float] = field(default_factory=dict)  # project -> 敞口权重 0..1

    def adjacency(self) -> dict[str, list[tuple[str, str, str]]]:
        graph: dict[str, list[tuple[str, str, str]]] = {}
        for source, target, kind, source_type in self.edges:
            if source_type == "node":
                source_key, target_key = _node_key(source), _project_key(target)
            else:
                source_key, target_key = _project_key(source), _project_key(target)
            graph.setdefault(source_key, []).append((target_key, kind, source))
            graph.setdefault(target_key, [])
        return graph

    def project_set(self) -> set[str]:
        return {_project_key(project) for project in self.projects}


@dataclass(frozen=True)
class Blocker:
    """单个项目的一条阻塞原因，携带可追溯的证据坐标。"""
    project_id: str
    node_id: str
    state: str
    origin: str
    paths: tuple[tuple[str, ...], ...]
    evidence_revision: int
    exposure: float
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "node_id": self.node_id, "state": self.state,
                "origin": self.origin, "paths": [list(path) for path in self.paths],
                "evidence_revision": self.evidence_revision, "exposure": self.exposure,
                "message": self.message}


@dataclass(frozen=True)
class ProjectRisk:
    project_id: str
    blocked: bool
    blockers: tuple[Blocker, ...]
    direct_nodes: tuple[str, ...]
    propagated_nodes: tuple[str, ...]
    cycle_refs: tuple[str, ...]
    exposure: float

    def as_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "blocked": self.blocked,
                "blockers": [item.as_dict() for item in self.blockers],
                "direct_nodes": list(self.direct_nodes),
                "propagated_nodes": list(self.propagated_nodes),
                "cycle_refs": list(self.cycle_refs), "exposure": self.exposure}


@dataclass(frozen=True)
class NodeFailure:
    """一个不可用根因节点的组合级影响，节点损失只在此计一次。"""
    node_id: str
    state: str
    evidence_revision: int
    direct_projects: tuple[str, ...]
    propagated_projects: tuple[str, ...]
    shared: bool
    exposure_sum: float
    paths: dict[str, tuple[tuple[str, ...], ...]]

    def as_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "state": self.state,
                "evidence_revision": self.evidence_revision,
                "direct_projects": list(self.direct_projects),
                "propagated_projects": list(self.propagated_projects),
                "shared": self.shared, "exposure_sum": self.exposure_sum,
                "paths": {key: [list(path) for path in value]
                          for key, value in sorted(self.paths.items())}}


@dataclass(frozen=True)
class PropagationReport:
    blocked_projects: tuple[str, ...]
    project_risks: tuple[ProjectRisk, ...]
    node_failures: tuple[NodeFailure, ...]
    cycles: tuple[tuple[str, ...], ...]
    # 组合敞口：每个项目只计一次其最大节点敞口（不沿路径叠加，避免重复放大）
    portfolio_exposure: float
    blocked_origin_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {"blocked_projects": list(self.blocked_projects),
                "project_risks": [item.as_dict() for item in self.project_risks],
                "node_failures": [item.as_dict() for item in self.node_failures],
                "cycles": [list(cycle) for cycle in self.cycles],
                "portfolio_exposure": self.portfolio_exposure,
                "blocked_origin_counts": dict(self.blocked_origin_counts)}

    def fingerprint(self) -> str:
        return stable_hash(self.as_dict())


def filter_report(report: PropagationReport, project_ids: Iterable[str]) -> PropagationReport:
    """把全量报告裁剪到指定项目范围：受限重评时与上次报告做同口径对比，

    避免把未进入本次审议范围的项目误报为“已解除阻塞”。
    """
    scope = {identifier(project) for project in project_ids}
    risks = tuple(risk for risk in report.project_risks if risk.project_id in scope)
    blocked = tuple(project for project in report.blocked_projects if project in scope)
    exposure_by_project = {risk.project_id: risk.exposure for risk in risks}
    failures: list[NodeFailure] = []
    for item in report.node_failures:
        direct = tuple(p for p in item.direct_projects if p in scope)
        propagated = tuple(p for p in item.propagated_projects if p in scope)
        affected = set(direct) | set(propagated)
        if not affected:
            continue
        failures.append(NodeFailure(
            node_id=item.node_id, state=item.state, evidence_revision=item.evidence_revision,
            direct_projects=direct, propagated_projects=propagated,
            shared=len(affected) > 1,
            exposure_sum=round(sum(exposure_by_project.get(p, 0.0) for p in affected), 12),
            paths={key: value for key, value in item.paths.items() if key in scope}))
    cycles = tuple(cycle for cycle in report.cycles if set(cycle) <= scope)
    origin_counts = {ORIGIN_DIRECT: 0, ORIGIN_PROPAGATED: 0, ORIGIN_MISSING: 0}
    for risk in risks:
        for blocker in risk.blockers:
            origin_counts[blocker.origin] += 1
    portfolio_exposure = round(sum(risk.exposure for risk in risks if risk.blocked), 12)
    return PropagationReport(
        blocked_projects=blocked, project_risks=risks,
        node_failures=tuple(failures), cycles=cycles,
        portfolio_exposure=portfolio_exposure, blocked_origin_counts=origin_counts)


def report_from_dict(payload: Mapping[str, Any]) -> PropagationReport:
    """从持久化的 dict 重建传导报告（与 as_dict 对称）。"""
    risks: list[ProjectRisk] = []
    for item in payload["project_risks"]:
        blockers = tuple(
            Blocker(project_id=blocker["project_id"], node_id=blocker["node_id"],
                    state=blocker["state"], origin=blocker["origin"],
                    paths=tuple(tuple(path) for path in blocker["paths"]),
                    evidence_revision=blocker["evidence_revision"],
                    exposure=blocker["exposure"], message=blocker["message"])
            for blocker in item["blockers"])
        risks.append(ProjectRisk(
            project_id=item["project_id"], blocked=item["blocked"], blockers=blockers,
            direct_nodes=tuple(item["direct_nodes"]),
            propagated_nodes=tuple(item["propagated_nodes"]),
            cycle_refs=tuple(item["cycle_refs"]), exposure=item["exposure"]))
    failures = tuple(
        NodeFailure(node_id=item["node_id"], state=item["state"],
                    evidence_revision=item["evidence_revision"],
                    direct_projects=tuple(item["direct_projects"]),
                    propagated_projects=tuple(item["propagated_projects"]),
                    shared=item["shared"], exposure_sum=item["exposure_sum"],
                    paths={key: tuple(tuple(path) for path in value)
                           for key, value in item["paths"].items()})
        for item in payload["node_failures"])
    return PropagationReport(
        blocked_projects=tuple(payload["blocked_projects"]),
        project_risks=tuple(risks), node_failures=failures,
        cycles=tuple(tuple(cycle) for cycle in payload["cycles"]),
        portfolio_exposure=payload["portfolio_exposure"],
        blocked_origin_counts=dict(payload["blocked_origin_counts"]))


def find_cycles(view: DependencyView) -> tuple[tuple[str, ...], ...]:
    """Tarjan 强连通分量：在项目-项目依赖子图中找出循环关系。

    自环（项目依赖自身）单独作为长度 1 的循环返回。
    """
    graph = view.adjacency()
    project_keys = view.project_set()
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    cycles: list[tuple[str, ...]] = []

    def neighbors(key: str) -> list[str]:
        return [target for target, _kind, _source in graph.get(key, ()) if target in project_keys]

    def strong_connect(vertex: str) -> None:
        indices[vertex] = index_counter[0]
        lowlink[vertex] = index_counter[0]
        index_counter[0] += 1
        stack.append(vertex)
        on_stack.add(vertex)
        for target in neighbors(vertex):
            if target not in indices:
                strong_connect(target)
                lowlink[vertex] = min(lowlink[vertex], lowlink[target])
            elif target in on_stack:
                lowlink[vertex] = min(lowlink[vertex], indices[target])
        if lowlink[vertex] == indices[vertex]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(_bare(member))
                if member == vertex:
                    break
            if len(component) > 1:
                cycles.append(tuple(sorted(component)))
            else:
                only = _project_key(component[0])
                if only in neighbors(only):
                    cycles.append((component[0],))

    for vertex in sorted(project_keys):
        if vertex not in indices:
            strong_connect(vertex)
    return tuple(sorted(cycles))


def _all_simple_paths(graph: Mapping[str, list[tuple[str, str, str]]],
                      start: str, end_projects: set[str], max_paths: int) -> list[list[str]]:
    """枚举从失效节点到受影响项目的简单路径（带条数上限，避免组合爆炸）。"""
    paths: list[list[str]] = []

    def walk(vertex: str, trail: list[str], visited: frozenset[str]) -> None:
        if len(paths) >= max_paths:
            return
        for target, kind, source in graph.get(vertex, ()):
            if target in visited:
                continue
            next_trail = trail + [_bare(target)]
            if target in end_projects:
                paths.append(next_trail)
                if len(paths) >= max_paths:
                    return
            walk(target, next_trail, visited | {target})

    walk(start, [_bare(start)], frozenset({start}))
    return paths


def propagate(view: DependencyView, max_paths_per_project: int = 8) -> PropagationReport:
    """计算依赖网络的风险传导结果。

    传导规则：失效/缺证节点 -> 直接要求它的项目 -> 该项目的下游项目，
    沿 upstream/shared 边继续传播；每条阻塞都保留完整路径供审议追溯。
    """
    graph = view.adjacency()
    project_keys = view.project_set()
    blocking_nodes = {node_id: state for node_id, state in view.nodes.items()
                      if state in BLOCKING_STATES}
    cycle_set = set(find_cycles(view))

    # project -> node_id -> Blocker，同一项目对同一节点只保留一条阻塞
    blockers_by_project: dict[str, dict[str, Blocker]] = {project: {} for project in view.projects}
    node_direct: dict[str, set[str]] = {}
    node_propagated: dict[str, set[str]] = {}
    node_paths: dict[str, dict[str, list[tuple[str, ...]]]] = {}

    for node_id in sorted(blocking_nodes):
        start = _node_key(node_id)
        if start not in graph:
            continue
        reachable = _all_simple_paths(graph, start, project_keys,
                                      max_paths=max_paths_per_project * max(1, len(view.projects)))
        if not reachable:
            continue
        node_direct[node_id] = set()
        node_propagated[node_id] = set()
        node_paths[node_id] = {}
        for path in reachable:
            project_id = path[-1]
            # 路径长度 2：node -> project 为直接依赖；更长则经过其他项目传导
            path_origin = ORIGIN_DIRECT if len(path) == 2 else ORIGIN_PROPAGATED
            if path_origin == ORIGIN_DIRECT:
                node_direct[node_id].add(project_id)
            else:
                node_propagated[node_id].add(project_id)
            node_paths[node_id].setdefault(project_id, [])
            if len(node_paths[node_id][project_id]) < max_paths_per_project:
                node_paths[node_id][project_id].append(tuple(path))
            state = blocking_nodes[node_id]
            # 缺证是独立类别；有证据但失效/过时则按直接或传导归类
            origin = ORIGIN_MISSING if state == STATE_MISSING else path_origin
            exposure = float(view.exposures.get(project_id, 1.0))
            message = (f"{project_id} {path_origin}-blocked by {node_id} ({state})"
                       if state != STATE_MISSING
                       else f"{project_id} missing_evidence on required dependency {node_id}")
            blockers_by_project.setdefault(project_id, {})
            existing = blockers_by_project[project_id].get(node_id)
            # 同一项目同一节点只保留一条：直接路径优先于传导，缺证不覆盖失效结论
            if existing is None or (existing.origin == ORIGIN_PROPAGATED
                                    and origin == ORIGIN_DIRECT):
                blockers_by_project[project_id][node_id] = Blocker(
                    project_id=project_id, node_id=node_id, state=state, origin=origin,
                    paths=(), evidence_revision=int(view.node_revisions.get(node_id, 0)),
                    exposure=exposure, message=message)

    project_risks: list[ProjectRisk] = []
    blocked: list[str] = []
    origin_counts = {ORIGIN_DIRECT: 0, ORIGIN_PROPAGATED: 0, ORIGIN_MISSING: 0}
    portfolio_exposure = 0.0
    for project_id in view.projects:
        block_map = blockers_by_project.get(project_id, {})
        # 回填每条阻塞的全部路径
        blockers: list[Blocker] = []
        direct_nodes: list[str] = []
        propagated_nodes: list[str] = []
        for node_id in sorted(block_map):
            item = block_map[node_id]
            paths = tuple(node_paths.get(node_id, {}).get(project_id, ()))
            blockers.append(Blocker(item.project_id, item.node_id, item.state, item.origin,
                                    paths, item.evidence_revision, item.exposure, item.message))
            if item.origin == ORIGIN_MISSING:
                # 缺证独立计数；它落在直接节点还是传导节点由路径决定
                origin_counts[ORIGIN_MISSING] += 1
                if node_id in node_direct:
                    direct_nodes.append(node_id)
                else:
                    propagated_nodes.append(node_id)
            elif item.origin == ORIGIN_DIRECT:
                direct_nodes.append(node_id)
                origin_counts[ORIGIN_DIRECT] += 1
            else:
                propagated_nodes.append(node_id)
                origin_counts[ORIGIN_PROPAGATED] += 1
        blockers.sort(key=lambda item: (item.node_id, item.origin))
        cycle_refs = tuple(cycle for cycle in sorted(cycle_set) if project_id in cycle)
        is_blocked = bool(blockers)
        if is_blocked:
            blocked.append(project_id)
        # 项目级敞口取该项目依赖边的最大敞口，与具体阻塞节点解耦；
        # 同一项目无论被多少节点/路径阻塞，组合敞口中只计这一次
        project_exposure = float(view.exposures.get(project_id, 1.0)) if is_blocked else 0.0
        portfolio_exposure += project_exposure
        project_risks.append(ProjectRisk(
            project_id=project_id, blocked=is_blocked, blockers=tuple(blockers),
            direct_nodes=tuple(sorted(set(direct_nodes))),
            propagated_nodes=tuple(sorted(set(propagated_nodes))),
            cycle_refs=cycle_refs, exposure=project_exposure))

    node_failures: list[NodeFailure] = []
    for node_id in sorted(node_direct):
        affected = node_direct[node_id] | node_propagated[node_id]
        exposure_sum = sum(float(view.exposures.get(project_id, 1.0)) for project_id in affected)
        node_failures.append(NodeFailure(
            node_id=node_id, state=blocking_nodes[node_id],
            evidence_revision=int(view.node_revisions.get(node_id, 0)),
            direct_projects=tuple(sorted(node_direct[node_id])),
            propagated_projects=tuple(sorted(node_propagated[node_id])),
            shared=len(affected) > 1, exposure_sum=exposure_sum,
            paths={key: tuple(value) for key, value in sorted(node_paths[node_id].items())}))

    return PropagationReport(
        blocked_projects=tuple(sorted(blocked)), project_risks=tuple(project_risks),
        node_failures=tuple(node_failures), cycles=tuple(sorted(cycle_set)),
        portfolio_exposure=round(portfolio_exposure, 12),
        blocked_origin_counts=origin_counts)


def simulate(view: DependencyView, overrides: Mapping[str, str]) -> PropagationReport:
    """情景推演：只在内存中假设部分节点状态变化，重新计算传导。

    overrides 不会写回任何台账，也不会改动项目实际阶段。
    """
    simulated_nodes = dict(view.nodes)
    for node_id, state in overrides.items():
        node_id = identifier(node_id)
        if node_id not in simulated_nodes:
            raise ContractError(f"unknown dependency node in scenario: {node_id}")
        simulated_nodes[node_id] = validate_state(state)
    simulated = DependencyView(nodes=simulated_nodes, node_revisions=dict(view.node_revisions),
                               edges=view.edges, projects=list(view.projects), exposures=dict(view.exposures))
    return propagate(simulated)


def review_delta(previous: PropagationReport | None, current: PropagationReport) -> dict[str, Any]:
    """解释本次审议相对上次真正改变了什么（只列实质变化，无变化显式说明）。"""
    previous_blocked = set(previous.blocked_projects) if previous else set()
    current_blocked = set(current.blocked_projects)
    previous_pairs = {(risk.project_id, blocker.node_id)
                      for risk in (previous.project_risks if previous else ())
                      for blocker in risk.blockers}
    current_pairs = {(risk.project_id, blocker.node_id)
                     for risk in current.project_risks for blocker in risk.blockers}
    previous_cycles = set(previous.cycles) if previous else set()
    previous_exposure = previous.portfolio_exposure if previous else 0.0
    return {
        "newly_blocked": sorted(current_blocked - previous_blocked),
        "cleared": sorted(previous_blocked - current_blocked),
        "still_blocked": sorted(previous_blocked & current_blocked),
        "blockers_added": sorted([list(pair) for pair in current_pairs - previous_pairs]),
        "blockers_removed": sorted([list(pair) for pair in previous_pairs - current_pairs]),
        "cycles_added": sorted([list(cycle) for cycle in set(current.cycles) - previous_cycles]),
        "cycles_cleared": sorted([list(cycle) for cycle in previous_cycles - set(current.cycles)]),
        "portfolio_exposure_delta": round(current.portfolio_exposure - previous_exposure, 12),
        "changed": bool(previous_blocked != current_blocked
                        or previous_pairs != current_pairs
                        or previous_cycles != set(current.cycles)
                        or previous_exposure != current.portfolio_exposure),
        "previous_fingerprint": previous.fingerprint() if previous else None,
        "current_fingerprint": current.fingerprint(),
    }
