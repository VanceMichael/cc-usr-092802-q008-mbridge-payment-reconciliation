"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .dayend import DayEndClose
from .payments import CrossBorderPayments
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def payment_demo(app: CivicFlow) -> dict:
    """横琴付款机构向澳门服务商付款的完整责任链演示。"""
    admin = AccessContext.system("demo-admin")
    payments = CrossBorderPayments.open(app)
    payments.register_profile(admin, org_id="org:hengqin", name="横琴付款机构", account="ACCT-HQ-001", jurisdiction="CN", request_key="demo-profile-payer")
    payments.register_profile(admin, org_id="org:macau", name="澳门服务商", account="ACCT-MO-889", jurisdiction="MO", request_key="demo-profile-payee")
    app.ledger.post(journal_key="funds:org:hengqin", account="available", currency="CNY", amount="50000.00", direction="debit", reference="fund:demo", actor="demo-admin")
    submitter = AccessContext(actor_id="hq-finance", permissions=frozenset({"write:payments", "read:payments"}), scopes=frozenset({"side:payer"}))
    approver = AccessContext(actor_id="hq-approver", permissions=frozenset({"approve:payments", "read:payments"}), scopes=frozenset({"side:payer"}))
    dispatcher = AccessContext(actor_id="hq-dispatcher", permissions=frozenset({"dispatch:payments", "read:payments"}), scopes=frozenset({"side:payer"}))
    bridge = AccessContext(actor_id="bridge-gw", permissions=frozenset({"ingest:bridge", "read:payments"}), scopes=frozenset({"side:payer", "side:payee"}))
    payee = AccessContext(actor_id="mo-service", permissions=frozenset({"read:payments"}), scopes=frozenset({"side:payee"}))
    inst = payments.submit(submitter, business_key="PAY-2026-0001", contract_id="HT-2026-118", acceptance_status="验收通过", payer_org="org:hengqin", payee_org="org:macau", components=[{"currency": "CNY", "amount": "12000.00", "fee": "120.00", "purpose": "服务费"}], request_key="demo-submit")
    inst = payments.approve(approver, "PAY-2026-0001", expected_version=inst["version"], reason="复核通过", request_key="demo-approve")
    inst = payments.dispatch(dispatcher, "PAY-2026-0001", expected_version=inst["version"], request_key="demo-dispatch")
    now = app.clock.now()
    payments.ingest(bridge, "PAY-2026-0001", sequence=1, kind="accepted", occurred_at=now)
    payments.ingest(bridge, "PAY-2026-0001", sequence=2, kind="cleared", occurred_at=now)
    payments.ingest(bridge, "PAY-2026-0001", sequence=3, kind="settled", currency="CNY", amount_minor=1188000, payee_account="ACCT-MO-889", occurred_at=now)
    closer = AccessContext(actor_id="hq-dayend", permissions=frozenset({"run:dayend", "read:payments"}), scopes=frozenset({"side:payer"}))
    runner = DayEndClose(app, payments)
    business_date = now[:10]
    runner.schedule(closer, business_date, run_at=now)
    day_end = runner.run_due(closer)
    return {
        "payer_view": payments.status(submitter, "PAY-2026-0001", as_of=now),
        "payee_view": payments.status(payee, "PAY-2026-0001"),
        "day_end": day_end,
        "verification": app.verify(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("payment-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "payment-demo": emit(payment_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
