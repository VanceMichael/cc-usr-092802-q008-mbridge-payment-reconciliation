"""日终关账：检查点驱动、可断点续跑的关账步骤。

关账由五个幂等步骤组成，每完成一步就在运行记录里留下检查点；处理中途
停机后，任务租约过期会被重新认领，恢复执行时跳过已完成步骤、续跑未完成
步骤。步骤本身全部幂等，崩溃发生在步骤中途也只会重放而不会重复生效。

步骤顺序：
1. ``sync_events`` 归并所有付款指令的桥侧事件；
2. ``recheck_profiles`` 复核机构资料与合规名单，变化即暂停；
3. ``cutoff_review`` 给当日未完成的指令打上结转标记；
4. ``snapshot_balances`` 为每个资金账簿留下余额快照（支持历史时点核对）；
5. ``close_books`` 汇总并封存当日运行记录。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import ConflictError, NotFoundError
from .identifiers import require_safe
from .jsonutil import digest_json
from .payments import ACCOUNTS, INSTRUCTION_TYPE, TERMINAL_STATES, CrossBorderPayments
from .repository import EntityRepository
from .security import AccessContext

RUN_TYPE = "day_end_runs"
JOB_TYPE = "day_end_close"
STEPS = ("sync_events", "recheck_profiles", "cutoff_review", "snapshot_balances", "close_books")


@dataclass(frozen=True)
class DayEndClose:
    """日终关账运行器；恢复时从未完成的第一个步骤继续。"""

    app: object
    payments: CrossBorderPayments

    @property
    def repository(self) -> EntityRepository:
        return self.app.repository

    def schedule(self, context: AccessContext, business_date: str, *, run_at: str) -> dict:
        context.require("run:dayend")
        require_safe(business_date, "关账日期")
        existing = self.repository.search(RUN_TYPE, "business_date", business_date)
        if existing:
            return existing[0]
        run = self.repository.create(RUN_TYPE, {"business_date": business_date, "completed_steps": [], "snapshots": [], "closed_at": "", "state": "scheduled"}, actor=context.actor_id, request_key=f"dayend:{business_date}")
        self.app.jobs.schedule(job_type=JOB_TYPE, subject_id=business_date, run_at=run_at, payload={"business_date": business_date})
        return run

    def run_due(self, context: AccessContext, *, limit: int = 5, lease_seconds: int = 30) -> list[dict]:
        """认领到期任务并执行；异常时交还队列等待重试，崩溃任务租约过期后会被重新认领。"""
        context.require("run:dayend")
        results = []
        for job in self.app.jobs.claim_due(seconds=lease_seconds, limit=limit):
            if job["job_type"] != JOB_TYPE:
                self.app.jobs.retry(job["job_id"], error="未知任务类型", retry_at=self.app.clock.now())
                continue
            business_date = json.loads(job["payload_json"])["business_date"]
            try:
                outcome = self._execute(business_date)
            except Exception as exc:
                self.app.jobs.retry(job["job_id"], error=str(exc), retry_at=self.app.clock.now())
                results.append({"business_date": business_date, "status": "retry", "error": str(exc)[:200]})
                continue
            self.app.jobs.finish(job["job_id"])
            results.append(outcome)
        return results

    def _execute(self, business_date: str) -> dict:
        system = AccessContext.system("day-end")
        while True:
            run = self._run(business_date)
            done = list(run["completed_steps"])
            remaining = [step for step in STEPS if step not in done]
            if not remaining:
                if run["state"] != "closed":
                    run = self.repository.update(RUN_TYPE, run["entity_id"], {"state": "closed", "closed_at": self.app.clock.now()}, actor="day-end", expected_version=run["version"], request_key=f"dayend:{business_date}:closed")
                return {"business_date": business_date, "status": "closed", "completed_steps": run["completed_steps"]}
            step = remaining[0]
            extra = self._execute_step(run, step, system) or {}
            changes = {"completed_steps": done + [step], **extra}
            try:
                self.repository.update(RUN_TYPE, run["entity_id"], changes, actor="day-end", expected_version=run["version"], request_key=f"dayend:{business_date}:checkpoint:{step}")
            except ConflictError:
                continue  # 并发运行者已推进检查点，重新读取继续

    def _execute_step(self, run: dict, step: str, system: AccessContext) -> dict | None:
        business_date = run["business_date"]
        if step == "sync_events":
            for inst in self._instructions():
                self.payments.sync(system, inst["business_key"])
            return None
        if step == "recheck_profiles":
            for inst in self._instructions():
                self.payments.recheck(system, inst["business_key"])
            return None
        if step == "cutoff_review":
            for inst in self._instructions():
                if inst["state"] in TERMINAL_STATES or inst.get("attention"):
                    continue
                self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], {"attention": "carry_over"}, actor="day-end", expected_version=inst["version"], request_key=f"dayend:{business_date}:cutoff:{inst['entity_id']}")
            return None
        if step == "snapshot_balances":
            snapshot_ids = list(run.get("snapshots", []))
            for journal, currencies in sorted(self._journals().items()):
                watermark = {currency: {account: self.app.ledger.account_balance(journal, account, currency=currency) for account in ACCOUNTS} for currency in sorted(currencies)}
                snapshot = self.repository.create("snapshots", {"subject_type": "journal", "subject_id": journal, "as_of": self.app.clock.now(), "watermark": watermark, "digest": digest_json(watermark), "state": "ready"}, actor="day-end", request_key=f"dayend:{business_date}:snapshot:{journal}")
                if snapshot["entity_id"] not in snapshot_ids:
                    snapshot_ids.append(snapshot["entity_id"])
            return {"snapshots": snapshot_ids}
        if step == "close_books":
            active = [inst for inst in self._instructions() if inst["state"] not in TERMINAL_STATES]
            return {"closed_summary": {"active_instructions": len(active), "journals": sorted(self._journals())}}
        raise NotFoundError(f"未知关账步骤: {step}")

    def _run(self, business_date: str) -> dict:
        rows = self.repository.search(RUN_TYPE, "business_date", business_date)
        if not rows:
            raise NotFoundError(f"关账运行 {business_date} 不存在")
        return rows[0]

    def _instructions(self) -> list[dict]:
        return self.repository.list(INSTRUCTION_TYPE, limit=500)

    def _journals(self) -> dict[str, set]:
        journals: dict[str, set] = {}
        for inst in self._instructions():
            journals.setdefault(self.payments.journal_of(inst), set()).update(inst["composition"].keys())
        return journals
