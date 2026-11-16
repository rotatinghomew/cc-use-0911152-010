from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from export_portfolio.portfolio_office import Application, run, verify


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='export_portfolio', description='人工智能东盟出海组合准备度与风险治理台')
    commands = root.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--fixture", required=True)
    execute.add_argument("--database", required=True)
    execute.add_argument("--output", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--database", required=True)
    check.add_argument("--output", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--database", required=True)

    review = commands.add_parser("dependency-review", help="沉淀一次依赖网络门禁审议")
    review.add_argument("--database", required=True)
    review.add_argument("--review-id", required=True)
    review.add_argument("--trigger", default="board-review")

    scenario = commands.add_parser("dependency-scenario", help="只读情景推演，不改实际阶段")
    scenario.add_argument("--database", required=True)
    scenario.add_argument("--overrides", required=True,
                          help='JSON 对象，形如 {"node-id":"evidenced"} 或 {"node-id":"failed"}')
    scenario.add_argument("--projects", default="", help="逗号分隔的项目范围，缺省为全量")

    trace = commands.add_parser("dependency-trace", help="沿阻塞原因追溯历次审议与证据")
    trace.add_argument("--database", required=True)
    trace.add_argument("--project", required=True)
    trace.add_argument("--node", required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "run":
        result = run(args.fixture, args.database, args.output)
    elif args.command == "verify":
        result = verify(args.database, args.output)
    elif args.command == "dependency-review":
        result = Application(args.database).board_dependency_review(args.review_id, args.trigger)
    elif args.command == "dependency-scenario":
        try:
            overrides = json.loads(args.overrides)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"overrides 必须是 JSON 对象: {exc}") from exc
        if not isinstance(overrides, dict):
            raise SystemExit("overrides 必须是 JSON 对象")
        projects = [item.strip() for item in args.projects.split(",") if item.strip()] or None
        result = Application(args.database).simulate_dependency_scenario(overrides, projects)
    elif args.command == "dependency-trace":
        result = Application(args.database).trace_dependency_blocker(args.project, args.node)
    else:
        result = Application(args.database).inspect()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
