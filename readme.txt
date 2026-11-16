人工智能东盟出海组合准备度与风险治理台

共同政策证据：Repository.register_policy 按事件身份保存同一政策证据及修订号，
link_policy_project 将多个项目关联同一事件；open_policy_review 固定项目证据版本、
政策修订和暴露，resolve_policy_review 拒绝在结论前被改过的证据；
policy_impact 可查询跨市场共享的项目和旧尽调。已决旧记录不因政策补报而改写。

运行验收：
python3 -m export_portfolio run --fixture fixtures/acceptance.json --database /tmp/export_portfolio.sqlite3 --output /tmp/export_portfolio-output

核验：
python3 -m export_portfolio verify --database /tmp/export_portfolio.sqlite3 --output /tmp/export_portfolio-output

全量测试：
python3 -m unittest discover -s tests -v

按模块测试：
python3 -m unittest tests.intake.test_initiative -v

构建检查：
python3 -m compileall -q export_portfolio tests

resource_book.capacity 登记共享伙伴或认证资源及承诺版本，require 登记项目需求，approve 在事务中合计占用并核对承诺；已批准项目需求和批准记录不可改写，inspect 可追溯当前剩余能力。

依赖网络风险传导（network/dependency_risks.py 为纯函数分析，governance/dependency_book.py 为台账）：
register_node 登记伙伴/认证/资源节点，新节点默认缺证（missing_evidence）；bind_evidence 绑定真实证据并按修订号留痕，
withdraw_evidence 撤回当前有效证据转为失效（failed），证据修订落后为过时（stale）；connect_project 登记项目对节点的直接
依赖（requires）或项目间上游依赖（upstream）。analyze/propagate 输出风险传导报告：
- direct（直接依赖失效）、propagated（沿项目链传导）、missing_evidence（缺证）、shared_failure（多项目共同依赖同一节点，
  节点损失只在 node_failures 计一次）四类分开；每条阻塞保留全部简单路径，多路径只保留一条阻塞但保留路径明细；
- Tarjan 强连通分量识别项目间循环（含自环），循环内失效继续传导且不无限扩散；
- 组合敞口按项目计一次（取项目依赖边最大敞口），不沿多条路径重复放大同一损失。
阶段门禁：decide 注入 dependency_report 后，依赖阻塞强制 hold，单项准备度再高也不能推进；阻塞按 project+node 独立存储，
解除一项不会误解除另一项。reevaluate_dependency_evidence 是真实证据变化的唯一入口：先沿网络求受影响项目闭包，
只重评这些待推进项目，delta 与上次报告按同一项目口径对比，说明 newly_blocked/cleared/still_blocked 与敞口增量；
gate_reviews 与 gate_review_blockers 不可改写，trace_dependency_blocker 可沿阻塞原因追到历次审议与证据修订。
simulate_dependency_scenario 为只读情景推演，结果不落库、不改项目实际阶段。

CLI 新增：
python3 -m export_portfolio dependency-review --database <db> --review-id <id> [--trigger <原因>]
python3 -m export_portfolio dependency-scenario --database <db> --overrides '{"节点":"evidenced"}' [--projects a,b]
python3 -m export_portfolio dependency-trace --database <db> --project <项目> --node <节点>

