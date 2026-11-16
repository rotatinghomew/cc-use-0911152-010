"""项目共享伙伴及认证能力的人工承诺与资源确认。"""
import json
from export_portfolio.intake.initiative import ContractError,canonical_json,stable_hash

class ResourceBook:
    def __init__(self, repository):
        self.repository=repository
        with repository.connect() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS delivery_resources(resource_id TEXT PRIMARY KEY,capacity INTEGER NOT NULL,revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS project_requirements(project_id TEXT NOT NULL,resource_id TEXT NOT NULL,units INTEGER NOT NULL,PRIMARY KEY(project_id,resource_id));
            CREATE TABLE IF NOT EXISTS resource_approvals(approval_id TEXT PRIMARY KEY,evidence_hash TEXT NOT NULL,project_json TEXT NOT NULL,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resource_allocations(approval_id TEXT NOT NULL,resource_id TEXT NOT NULL,units INTEGER NOT NULL,PRIMARY KEY(approval_id,resource_id));""")

    def capacity(self,resource_id,units):
        if not resource_id or type(units) is not int or units<0: raise ContractError("资源承诺无效")
        with self.repository.transaction() as c:
            old=c.execute("SELECT * FROM delivery_resources WHERE resource_id=?",(resource_id,)).fetchone()
            occupied=c.execute("SELECT COALESCE(SUM(units),0) FROM resource_allocations WHERE resource_id=?",(resource_id,)).fetchone()[0]
            if units<occupied: raise ContractError("承诺不能小于已执行分配")
            if old and old['capacity']==units:return old['revision']
            rev=old['revision']+1 if old else 1
            c.execute("INSERT INTO delivery_resources VALUES(?,?,?) ON CONFLICT(resource_id) DO UPDATE SET capacity=excluded.capacity,revision=excluded.revision",(resource_id,units,rev));return rev

    def require(self,project_id,resource_id,units):
        if type(units) is not int or units<1:raise ContractError("需求数量无效")
        with self.repository.transaction() as c:
            if not c.execute("SELECT 1 FROM records WHERE record_id=?",(project_id,)).fetchone() or not c.execute("SELECT 1 FROM delivery_resources WHERE resource_id=?",(resource_id,)).fetchone():raise ContractError("项目或资源不存在")
            for row in c.execute("SELECT project_json FROM resource_approvals WHERE status='approved'"):
                if project_id in json.loads(row[0]):raise ContractError("已批准项目需求不可改写")
            c.execute("INSERT INTO project_requirements VALUES(?,?,?) ON CONFLICT(project_id,resource_id) DO UPDATE SET units=excluded.units",(project_id,resource_id,units))

    def approve(self,approval_id,project_ids,revisions):
        if not approval_id or not project_ids or len(project_ids)!=len(set(project_ids)):raise ContractError("批准须有独立项目")
        with self.repository.transaction() as c:
            projects={};totals={}
            for pid in sorted(project_ids):
                row=c.execute("SELECT fingerprint FROM records WHERE record_id=?",(pid,)).fetchone()
                if row is None:raise ContractError("项目不存在")
                req=[dict(r) for r in c.execute("SELECT resource_id,units FROM project_requirements WHERE project_id=? ORDER BY resource_id",(pid,))]
                if not req:raise ContractError("项目未登记资源需求")
                projects[pid]={'evidence':row[0],'requirements':req}
                for r in req:totals[r['resource_id']]=totals.get(r['resource_id'],0)+r['units']
            fingerprint=stable_hash({'projects':projects,'revisions':revisions})
            old=c.execute("SELECT * FROM resource_approvals WHERE approval_id=?",(approval_id,)).fetchone()
            if old:
                if old['evidence_hash']!=fingerprint:raise ContractError("批准历史不可覆盖")
                return dict(old)
            for approval in c.execute("SELECT project_json FROM resource_approvals WHERE status='approved'"):
                if set(project_ids)&set(json.loads(approval[0])):raise ContractError("项目已经占用资源")
            for resource,units in totals.items():
                row=c.execute("SELECT * FROM delivery_resources WHERE resource_id=?",(resource,)).fetchone()
                occupied=c.execute("SELECT COALESCE(SUM(units),0) FROM resource_allocations WHERE resource_id=?",(resource,)).fetchone()[0]
                if revisions.get(resource)!=row['revision'] or occupied+units>row['capacity']:raise ContractError("承诺已变化或共享能力不足")
            c.execute("INSERT INTO resource_approvals VALUES(?,?,?,'approved')",(approval_id,fingerprint,canonical_json(projects)))
            for resource,units in totals.items():c.execute("INSERT INTO resource_allocations VALUES(?,?,?)",(approval_id,resource,units))
            return dict(c.execute("SELECT * FROM resource_approvals WHERE approval_id=?",(approval_id,)).fetchone())

    def inspect(self):
        with self.repository.connect() as c:
            resources=[dict(r) for r in c.execute("SELECT * FROM delivery_resources ORDER BY resource_id")]
            for r in resources:r['available']=r['capacity']-c.execute("SELECT COALESCE(SUM(units),0) FROM resource_allocations WHERE resource_id=?",(r['resource_id'],)).fetchone()[0]
            return {'resources':resources,'approvals':[dict(r) for r in c.execute("SELECT * FROM resource_approvals ORDER BY approval_id")],'requirements':[dict(r) for r in c.execute("SELECT * FROM project_requirements ORDER BY project_id,resource_id")]}
