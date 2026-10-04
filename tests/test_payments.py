from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.dayend import STEPS, DayEndClose
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.payments import CrossBorderPayments
from civicflow.security import AccessContext


def ctx(actor, permissions, scopes=("side:payer",)):
    return AccessContext(actor_id=actor, permissions=frozenset(permissions), scopes=frozenset(scopes))


class PaymentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.sqlite3"
        self.app = CivicFlow.open(self.db, fixed_now="2026-09-28T09:00:00+08:00")
        self.payments = CrossBorderPayments.open(self.app)
        self.admin = AccessContext.system("admin")
        self.submitter = ctx("hq-finance", {"write:payments", "read:payments", "history:payments"})
        self.approver = ctx("hq-approver", {"approve:payments", "read:payments"})
        self.approver_b = ctx("hq-approver-b", {"approve:payments", "read:payments"})
        self.dispatcher = ctx("hq-dispatcher", {"dispatch:payments", "read:payments"})
        self.bridge = ctx("bridge-gw", {"ingest:bridge", "read:payments"}, ("side:payer", "side:payee"))
        self.payee = ctx("mo-service", {"read:payments"}, ("side:payee",))
        self.compliance = ctx("hq-compliance", {"release:payments", "read:payments"}, ("side:payer", "side:payee"))
        self.reconciler = ctx("hq-reconciler", {"resolve:reconciliation", "read:payments", "correct:payments"})
        self.closer = ctx("hq-dayend", {"run:dayend", "read:payments"})
        self.auditor = ctx("auditor", {"read:payments", "history:payments"}, ("*",))
        self.payments.register_profile(self.admin, org_id="org:hengqin", name="横琴付款机构", account="ACCT-HQ-001", jurisdiction="CN")
        self.payments.register_profile(self.admin, org_id="org:macau", name="澳门服务商", account="ACCT-MO-889", jurisdiction="MO")
        self.app.ledger.post(journal_key="funds:org:hengqin", account="available", currency="CNY", amount="50000.00", direction="debit", reference="fund:initial:CNY", actor="admin")
        self.app.ledger.post(journal_key="funds:org:hengqin", account="available", currency="MOP", amount="20000.00", direction="debit", reference="fund:initial:MOP", actor="admin")

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, now):
        app = CivicFlow.open(self.db, fixed_now=now)
        return app, CrossBorderPayments.open(app)

    def submit_default(self, **overrides):
        values = dict(
            business_key="PAY-0001",
            contract_id="HT-2026-118",
            acceptance_status="验收通过",
            payer_org="org:hengqin",
            payee_org="org:macau",
            components=[
                {"currency": "CNY", "amount": "12000.00", "fee": "120.00", "purpose": "服务费"},
                {"currency": "CNY", "amount": "3000.00", "fee": "0", "purpose": "杂项"},
            ],
        )
        values.update(overrides)
        return self.payments.submit(self.submitter, **values)

    def drive_to_dispatched(self, **overrides):
        inst = self.submit_default(**overrides)
        inst = self.payments.approve(self.approver, inst["business_key"], expected_version=inst["version"], reason="复核通过")
        return self.payments.dispatch(self.dispatcher, inst["business_key"], expected_version=inst["version"])

    def settle_events(self, key):
        self.payments.ingest(self.bridge, key, sequence=1, kind="accepted", occurred_at="2026-09-28T10:00:00+08:00")
        self.payments.ingest(self.bridge, key, sequence=2, kind="cleared", occurred_at="2026-09-28T10:05:00+08:00")
        self.payments.ingest(self.bridge, key, sequence=3, kind="settled", currency="CNY", amount_minor=1488000, payee_account="ACCT-MO-889", occurred_at="2026-09-28T10:10:00+08:00")

    def drive_to_settled(self, **overrides):
        inst = self.drive_to_dispatched(**overrides)
        self.settle_events(inst["business_key"])
        return self.payments.get_instruction(self.auditor, inst["business_key"])

    # ------------------------------------------------------------------
    # 提交：固化验收状态、双方资料、业务水位与付款组成
    # ------------------------------------------------------------------

    def test_submit_freezes_acceptance_profiles_waterlevel_and_composition(self):
        inst = self.submit_default()
        self.assertEqual(inst["state"], "submitted")
        self.assertEqual(inst["acceptance_status"], "验收通过")
        self.assertEqual(inst["payer_profile"]["account"], "ACCT-HQ-001")
        self.assertEqual(inst["payee_profile"]["account"], "ACCT-MO-889")
        self.assertEqual(inst["water_level"], {"CNY": 5000000})
        self.assertEqual(inst["composition"]["CNY"], {"gross_minor": 1500000, "fee_minor": 12000, "net_minor": 1488000})
        self.assertEqual(len(inst["components"]), 2)
        # 提交后资金与资料变化不影响已固化内容
        self.app.ledger.post(journal_key="funds:org:hengqin", account="available", currency="CNY", amount="1000.00", direction="debit", reference="fund:later", actor="admin")
        profile = self.payments._current_profile("org:macau")
        self.payments.update_profile(self.admin, "org:macau", {"account": "ACCT-MO-999"}, expected_version=profile["version"], request_key="profile-change-1")
        frozen = self.payments.get_instruction(self.auditor, "PAY-0001")
        self.assertEqual(frozen["water_level"], {"CNY": 5000000})
        self.assertEqual(frozen["payee_profile"]["account"], "ACCT-MO-889")

    def test_submit_validation(self):
        with self.assertRaises(ValidationError):
            self.submit_default(acceptance_status="验收中")
        with self.assertRaises(ValidationError):
            self.submit_default(components=[])
        with self.assertRaises(ValidationError):
            self.submit_default(components=[{"currency": "CNY", "amount": "100.00", "fee": "100.00"}])
        with self.assertRaises(ValidationError):
            self.submit_default(components=[{"currency": "CNY", "amount": "abc", "fee": "0"}])
        with self.assertRaises(ValidationError):
            self.submit_default(components=[{"currency": "CNY", "amount": "60000.00", "fee": "0"}])
        with self.assertRaises(PermissionDenied):
            self.payments.submit(self.payee, business_key="PAY-X", contract_id="HT-1", acceptance_status="验收通过", payer_org="org:hengqin", payee_org="org:macau", components=[{"currency": "CNY", "amount": "1.00", "fee": "0"}])

    def test_submit_idempotent_under_replay_and_concurrency(self):
        first = self.submit_default()
        second = self.submit_default()
        self.assertEqual(first["entity_id"], second["entity_id"])
        with self.assertRaises(ConflictError):
            self.submit_default(memo="另一笔付款")
        barrier = threading.Barrier(5)
        results, errors = [], []

        def work():
            barrier.wait()
            try:
                results.append(self.submit_default()["entity_id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(set(results), {first["entity_id"]})
        rows = self.app.repository.search("payment_instructions", "business_key", "PAY-0001")
        self.assertEqual(len(rows), 1)

    # ------------------------------------------------------------------
    # 审批：职责分离与并发批准
    # ------------------------------------------------------------------

    def test_submitter_cannot_self_approve(self):
        inst = self.submit_default()
        both = ctx("hq-finance", {"write:payments", "approve:payments", "read:payments"})
        with self.assertRaises(PermissionDenied):
            self.payments.approve(both, "PAY-0001", expected_version=inst["version"], reason="自审")
        with self.assertRaises(PermissionDenied):
            self.payments.approve(self.submitter, "PAY-0001", expected_version=inst["version"], reason="越权")

    def test_concurrent_approval_allows_single_winner(self):
        inst = self.submit_default()
        approved = self.payments.approve(self.approver, "PAY-0001", expected_version=inst["version"], reason="A 批准")
        with self.assertRaises(ConflictError):
            self.payments.approve(self.approver_b, "PAY-0001", expected_version=inst["version"], reason="B 批准")
        final = self.payments.get_instruction(self.submitter, "PAY-0001")
        self.assertEqual(final["approved_by"], approved["approved_by"])
        self.assertEqual(final["state"], "approved")

    def test_threaded_approval_allows_single_winner(self):
        inst = self.submit_default()
        barrier = threading.Barrier(2)
        winners, losers = [], []

        def call(who):
            barrier.wait()
            try:
                winners.append(self.payments.approve(who, "PAY-0001", expected_version=inst["version"], reason="批准")["approved_by"])
            except ConflictError:
                losers.append(who.actor_id)

        threads = [threading.Thread(target=call, args=(who,)) for who in (self.approver, self.approver_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        final = self.payments.get_instruction(self.submitter, "PAY-0001")
        self.assertEqual(final["approved_by"], winners[0])

    # ------------------------------------------------------------------
    # 桥侧事件归并：顺序、重放、资金落点
    # ------------------------------------------------------------------

    def test_happy_path_events_fold_and_fund_location(self):
        self.drive_to_dispatched()
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "available", currency="CNY"), 3500000)
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "in_transit", currency="CNY"), 1500000)
        self.settle_events("PAY-0001")
        inst = self.payments.get_instruction(self.auditor, "PAY-0001")
        self.assertEqual(inst["state"], "settled")
        status = self.payments.status(self.submitter, "PAY-0001")
        self.assertEqual(status["fund_location"]["CNY"], {"available": -1500000, "in_transit": 0, "settled_out": 1488000, "fee_income": 12000})
        self.assertEqual([e["kind"] for e in status["progress"]["applied"]], ["accepted", "cleared", "settled"])
        self.assertEqual(status["responsible"]["role"], "closed")
        self.assertEqual(status["settlement_summary"]["CNY"]["outstanding_minor"], 1488000)
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "available", currency="CNY"), 3500000)
        self.assertGreater(self.app.verify()["audit_entries"], 0)

    def test_multi_currency_settles_currency_by_currency(self):
        components = [
            {"currency": "CNY", "amount": "1000.00", "fee": "0", "purpose": "服务费"},
            {"currency": "MOP", "amount": "500.00", "fee": "5.00", "purpose": "跨境费"},
        ]
        inst = self.drive_to_dispatched(components=components)
        key = inst["business_key"]
        self.payments.ingest(self.bridge, key, sequence=1, kind="accepted", occurred_at="2026-09-28T10:00:00+08:00")
        self.payments.ingest(self.bridge, key, sequence=2, kind="cleared", occurred_at="2026-09-28T10:05:00+08:00")
        self.payments.ingest(self.bridge, key, sequence=3, kind="settled", currency="CNY", amount_minor=100000, payee_account="ACCT-MO-889", occurred_at="2026-09-28T10:06:00+08:00")
        inst = self.payments.get_instruction(self.submitter, key)
        self.assertEqual(inst["state"], "settling")
        self.payments.ingest(self.bridge, key, sequence=4, kind="settled", currency="MOP", amount_minor=49500, payee_account="ACCT-MO-889", occurred_at="2026-09-28T10:07:00+08:00")
        inst = self.payments.get_instruction(self.submitter, key)
        self.assertEqual(inst["state"], "settled")
        status = self.payments.status(self.submitter, key)
        self.assertEqual(status["fund_location"]["MOP"], {"available": -50000, "in_transit": 0, "settled_out": 49500, "fee_income": 500})

    def test_out_of_order_events_wait_for_missing_sequence(self):
        self.drive_to_dispatched()
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=2, kind="cleared", occurred_at="2026-09-28T10:05:00+08:00")
        self.assertEqual(result["fold"]["applied"], [])
        self.assertEqual(result["fold"]["pending"], [2])
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=1, kind="accepted", occurred_at="2026-09-28T10:00:00+08:00")
        self.assertEqual(result["fold"]["applied"], [1, 2])
        self.assertEqual(result["fold"]["state"], "cleared")

    def test_event_replay_and_conflicting_sequence(self):
        self.drive_to_dispatched()
        first = self.payments.ingest(self.bridge, "PAY-0001", sequence=1, kind="accepted", occurred_at="2026-09-28T10:00:00+08:00")
        replay = self.payments.ingest(self.bridge, "PAY-0001", sequence=1, kind="accepted", occurred_at="2026-09-28T10:00:00+08:00")
        self.assertEqual(first["inbox"]["status"], "accepted")
        self.assertEqual(replay["inbox"]["status"], "duplicate")
        inst = self.payments.get_instruction(self.submitter, "PAY-0001")
        self.assertEqual(len(inst["event_trail"]), 1)
        again = self.payments.sync(self.bridge, "PAY-0001")
        self.assertEqual(again["applied"], [])
        with self.assertRaises(ConflictError):
            self.payments.ingest(self.bridge, "PAY-0001", sequence=1, kind="cleared", occurred_at="2026-09-28T10:01:00+08:00")
        status = self.payments.status(self.submitter, "PAY-0001")
        self.assertEqual(len(status["errors"]["inbox_conflicts"]), 1)

    # ------------------------------------------------------------------
    # 迟到回执与核对队列：不得自动补付
    # ------------------------------------------------------------------

    def test_late_settled_receipt_never_double_pays(self):
        self.drive_to_settled()
        before = len(self.app.ledger.entries("funds:org:hengqin"))
        # 迟到且内容一致的回执：只记录，不产生新分录
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=4, kind="settled", currency="CNY", amount_minor=1488000, payee_account="ACCT-MO-889", occurred_at="2026-09-28T11:00:00+08:00")
        self.assertEqual(result["fold"]["state"], "settled")
        inst = self.payments.get_instruction(self.submitter, "PAY-0001")
        self.assertEqual(inst["event_trail"][-1]["note"], "late_duplicate")
        self.assertEqual(len(self.app.ledger.entries("funds:org:hengqin")), before)
        # 迟到且金额矛盾的回执：进入核对队列，不动资金
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=5, kind="settled", currency="CNY", amount_minor=1488001, payee_account="ACCT-MO-889", occurred_at="2026-09-28T11:05:00+08:00")
        self.assertEqual(result["fold"]["state"], "reconciling")
        self.assertEqual(len(self.app.ledger.entries("funds:org:hengqin")), before)
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "available", currency="CNY"), 3500000)
        queue = self.payments.reconciliation_queue(self.reconciler)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["category"], "amount_mismatch")
        status = self.payments.status(self.reconciler, "PAY-0001")
        self.assertEqual(status["responsible"]["role"], "reconciler")
        self.assertEqual(status["errors"]["reconciliation_open"][0]["entity_id"], queue[0]["entity_id"])
        resolved = self.payments.resolve_reconciliation(self.reconciler, queue[0]["entity_id"], action="confirm_existing", reason="以首笔回执为准")
        self.assertEqual(resolved["item"]["state"], "resolved")
        self.assertEqual(self.payments.get_instruction(self.submitter, "PAY-0001")["state"], "settled")

    def test_accept_incoming_posts_delta_adjustment_only(self):
        self.drive_to_dispatched()
        self.payments.ingest(self.bridge, "PAY-0001", sequence=1, kind="accepted", occurred_at="2026-09-28T10:00:00+08:00")
        self.payments.ingest(self.bridge, "PAY-0001", sequence=2, kind="cleared", occurred_at="2026-09-28T10:05:00+08:00")
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=3, kind="settled", currency="CNY", amount_minor=1400000, payee_account="ACCT-MO-889", occurred_at="2026-09-28T10:10:00+08:00")
        self.assertEqual(result["fold"]["state"], "reconciling")
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "in_transit", currency="CNY"), 1500000)
        item = self.payments.reconciliation_queue(self.reconciler)[0]
        self.payments.resolve_reconciliation(self.reconciler, item["entity_id"], action="accept_incoming", reason="桥侧确认实际结算 14000.00")
        inst = self.payments.get_instruction(self.submitter, "PAY-0001")
        self.assertEqual(inst["state"], "settled")
        self.assertEqual(inst["settled"]["CNY"]["net_minor"], 1400000)
        status = self.payments.status(self.submitter, "PAY-0001")
        self.assertEqual(status["fund_location"]["CNY"]["settled_out"], 1400000)
        self.assertEqual(status["fund_location"]["CNY"]["reconciliation_adjustment"], 100000)

    def test_returned_then_manual_repay_then_late_original_receipt(self):
        self.drive_to_dispatched()
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=1, kind="returned", occurred_at="2026-09-28T10:00:00+08:00")
        self.assertEqual(result["fold"]["state"], "returned")
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "available", currency="CNY"), 5000000)
        # 人工判断后改用新业务标识补付，原指令不做任何自动处理
        repaid = self.drive_to_settled(business_key="PAY-0002")
        self.assertEqual(repaid["state"], "settled")
        # 原回执迟到到达：进入核对队列，不得再次动账造成双付
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=2, kind="settled", currency="CNY", amount_minor=1488000, payee_account="ACCT-MO-889", occurred_at="2026-09-28T12:00:00+08:00")
        self.assertEqual(result["fold"]["state"], "reconciling")
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "available", currency="CNY"), 3500000)
        refs = [e["reference"] for e in self.app.ledger.entries("funds:org:hengqin")]
        self.assertNotIn("PAY-0001:settled:CNY:net", refs)
        item = self.payments.reconciliation_queue(self.reconciler)[0]
        self.payments.resolve_reconciliation(self.reconciler, item["entity_id"], action="confirm_existing", reason="已人工补付，原回执作废")
        self.assertEqual(self.payments.get_instruction(self.submitter, "PAY-0001")["state"], "returned")
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "available", currency="CNY"), 3500000)

    def test_malformed_receipt_goes_to_queue_not_poison(self):
        self.drive_to_dispatched()
        self.payments.ingest(self.bridge, "PAY-0001", sequence=1, kind="accepted", occurred_at="2026-09-28T10:00:00+08:00")
        self.payments.ingest(self.bridge, "PAY-0001", sequence=2, kind="cleared", occurred_at="2026-09-28T10:05:00+08:00")
        # 缺金额的结算回执：进入核对队列而不是卡死归并
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=3, kind="settled", currency="CNY", occurred_at="2026-09-28T10:10:00+08:00")
        self.assertEqual(result["fold"]["state"], "reconciling")
        item = self.payments.reconciliation_queue(self.reconciler)[0]
        resolved = self.payments.resolve_reconciliation(self.reconciler, item["entity_id"], action="confirm_existing", reason="桥侧报文残缺，驳回待重发")
        self.assertEqual(resolved["item"]["state"], "resolved")
        # 队列恢复后，后续正确回执照常归并
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=4, kind="settled", currency="CNY", amount_minor=1488000, payee_account="ACCT-MO-889", occurred_at="2026-09-28T10:20:00+08:00")
        self.assertEqual(result["fold"]["state"], "settled")
        # 重复处理同一核对项：幂等返回，不重复落账
        replay = self.payments.resolve_reconciliation(self.reconciler, item["entity_id"], action="confirm_existing", reason="重复调用")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ConflictError):
            self.payments.resolve_reconciliation(self.reconciler, item["entity_id"], action="accept_incoming", reason="矛盾的处理")

    # ------------------------------------------------------------------
    # 部分退款与冲正
    # ------------------------------------------------------------------

    def test_partial_refunds_accumulate_and_over_refund_goes_to_queue(self):
        self.drive_to_settled()
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=4, kind="refunded", currency="CNY", amount_minor=30000, occurred_at="2026-09-29T09:00:00+08:00")
        self.assertEqual(result["fold"]["state"], "settled")
        inst = self.payments.get_instruction(self.submitter, "PAY-0001")
        self.assertEqual(inst["refunded"], {"CNY": 30000})
        self.assertEqual(self.app.ledger.account_balance("funds:org:hengqin", "available", currency="CNY"), 3530000)
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=5, kind="refunded", currency="CNY", amount_minor=1458000, occurred_at="2026-09-29T10:00:00+08:00")
        self.assertEqual(result["fold"]["state"], "refunded")
        status = self.payments.status(self.submitter, "PAY-0001")
        self.assertEqual(status["settlement_summary"]["CNY"]["outstanding_minor"], 0)
        # 超额退款进入核对队列，不动资金
        before = len(self.app.ledger.entries("funds:org:hengqin"))
        result = self.payments.ingest(self.bridge, "PAY-0001", sequence=6, kind="refunded", currency="CNY", amount_minor=1, occurred_at="2026-09-29T11:00:00+08:00")
        self.assertEqual(result["fold"]["state"], "reconciling")
        self.assertEqual(len(self.app.ledger.entries("funds:org:hengqin")), before)
        item = self.payments.reconciliation_queue(self.reconciler)[0]
        self.assertEqual(item["category"], "over_refund")
        self.payments.resolve_reconciliation(self.reconciler, item["entity_id"], action="confirm_existing", reason="桥侧误发，驳回")
        self.assertEqual(self.payments.get_instruction(self.submitter, "PAY-0001")["state"], "refunded")

    def test_reversal_only_after_settlement_and_idempotent(self):
        self.drive_to_dispatched()
        entry = next(e for e in self.app.ledger.entries("funds:org:hengqin") if e["reference"] == "PAY-0001:dispatch:CNY:in")
        with self.assertRaises(ConflictError):
            self.payments.correct(self.reconciler, "PAY-0001", entry_id=entry["entry_id"], reason="未结算不能冲正")
        self.settle_events("PAY-0001")
        entry = next(e for e in self.app.ledger.entries("funds:org:hengqin") if e["reference"] == "PAY-0001:settled:CNY:net")
        first = self.payments.correct(self.reconciler, "PAY-0001", entry_id=entry["entry_id"], reason="误付冲正")
        replay = self.payments.correct(self.reconciler, "PAY-0001", entry_id=entry["entry_id"], reason="误付冲正")
        self.assertEqual(first["reversal"]["entry_id"], replay["reversal"]["entry_id"])
        status = self.payments.status(self.submitter, "PAY-0001")
        self.assertEqual(status["fund_location"]["CNY"]["settled_out"], 0)
        self.assertEqual(len(status["corrections"]), 1)
        original = next(e for e in self.app.ledger.entries("funds:org:hengqin") if e["entry_id"] == entry["entry_id"])
        self.assertEqual(original["amount_minor"], 1488000)
        with self.assertRaises(NotFoundError):
            self.payments.correct(self.reconciler, "PAY-0001", entry_id="entry:nonexistent", reason="不存在")

    # ------------------------------------------------------------------
    # 资料变化与合规命中暂停
    # ------------------------------------------------------------------

    def test_profile_change_pauses_and_resume_refreezes(self):
        inst = self.submit_default()
        inst = self.payments.approve(self.approver, "PAY-0001", expected_version=inst["version"], reason="复核通过")
        profile = self.payments._current_profile("org:macau")
        self.payments.update_profile(self.admin, "org:macau", {"account": "ACCT-MO-999"}, expected_version=profile["version"], request_key="profile-change-2")
        held = self.payments.dispatch(self.dispatcher, "PAY-0001", expected_version=inst["version"])
        self.assertEqual(held["state"], "on_hold")
        self.assertEqual(held["hold_reason"], "profile_changed:payee")
        with self.assertRaises(ConflictError):
            self.payments.dispatch(self.dispatcher, "PAY-0001", expected_version=held["version"])
        resumed = self.payments.resume(self.compliance, "PAY-0001", expected_version=held["version"])
        self.assertEqual(resumed["state"], "approved")
        self.assertEqual(resumed["payee_profile"]["account"], "ACCT-MO-999")
        self.assertEqual(len(resumed["profile_refreshes"]), 1)
        dispatched = self.payments.dispatch(self.dispatcher, "PAY-0001", expected_version=resumed["version"])
        self.assertEqual(dispatched["state"], "dispatched")
        history = self.payments.history(self.auditor, "PAY-0001")
        self.assertEqual(history[0]["payee_profile"]["account"], "ACCT-MO-889")

    def test_compliance_hit_pauses_until_cleared_or_overridden(self):
        sanctioned = CrossBorderPayments.open(self.app, sanctioned={"ACCT-MO-889"})
        inst = sanctioned.submit(self.submitter, business_key="PAY-0001", contract_id="HT-2026-118", acceptance_status="验收通过", payer_org="org:hengqin", payee_org="org:macau", components=[{"currency": "CNY", "amount": "100.00", "fee": "0"}])
        self.assertEqual(inst["state"], "on_hold")
        self.assertTrue(inst["hold_reason"].startswith("compliance:"))
        with self.assertRaises(ConflictError):
            sanctioned.approve(self.approver, "PAY-0001", expected_version=inst["version"], reason="试图批准")
        with self.assertRaises(ConflictError):
            sanctioned.resume(self.compliance, "PAY-0001", expected_version=inst["version"])
        resumed = sanctioned.resume(self.compliance, "PAY-0001", expected_version=inst["version"], override_reason="白名单复核通过")
        self.assertEqual(resumed["state"], "submitted")
        inst2 = sanctioned.submit(self.submitter, business_key="PAY-0002", contract_id="HT-2026-119", acceptance_status="验收通过", payer_org="org:hengqin", payee_org="org:macau", components=[{"currency": "CNY", "amount": "100.00", "fee": "0"}])
        cleared = CrossBorderPayments.open(self.app)
        resumed = cleared.resume(self.compliance, "PAY-0002", expected_version=inst2["version"])
        self.assertEqual(resumed["state"], "submitted")

    # ------------------------------------------------------------------
    # 字段可见范围
    # ------------------------------------------------------------------

    def test_role_based_field_visibility(self):
        self.drive_to_settled()
        payee_view = self.payments.status(self.payee, "PAY-0001")
        self.assertEqual(payee_view["fund_location"], "***")
        self.assertEqual(payee_view["instruction"]["water_level"], "***")
        self.assertEqual(payee_view["instruction"]["payer_profile"]["account"], "***")
        self.assertEqual(payee_view["instruction"]["composition"]["CNY"]["fee_minor"], "***")
        self.assertEqual(payee_view["instruction"]["payee_profile"]["account"], "ACCT-MO-889")
        self.assertEqual(payee_view["settlement_summary"]["CNY"]["net_minor"], 1488000)
        self.assertNotIn("fee_minor", payee_view["settlement_summary"]["CNY"])
        payer_view = self.payments.status(self.submitter, "PAY-0001")
        self.assertEqual(payer_view["instruction"]["payee_profile"]["account"], "***")
        self.assertEqual(payer_view["instruction"]["water_level"], {"CNY": 5000000})
        self.assertEqual(payer_view["instruction"]["composition"]["CNY"]["fee_minor"], 12000)
        auditor = ctx("auditor", {"read:payments"}, ("*",))
        audit_view = self.payments.status(auditor, "PAY-0001")
        self.assertEqual(audit_view["instruction"]["payer_profile"]["account"], "ACCT-HQ-001")
        self.assertEqual(audit_view["instruction"]["payee_profile"]["account"], "ACCT-MO-889")

    # ------------------------------------------------------------------
    # 统一查询与历史时点
    # ------------------------------------------------------------------

    def test_unified_status_query_and_historical_balance(self):
        app1, payments1 = self.reopen("2026-09-28T10:00:00+08:00")
        inst = payments1.submit(self.submitter, business_key="PAY-0001", contract_id="HT-2026-118", acceptance_status="验收通过", payer_org="org:hengqin", payee_org="org:macau", components=[{"currency": "CNY", "amount": "15000.00", "fee": "120.00"}])
        app2, payments2 = self.reopen("2026-09-28T10:30:00+08:00")
        inst = payments2.approve(self.approver, "PAY-0001", expected_version=inst["version"], reason="复核通过")
        app3, payments3 = self.reopen("2026-09-28T11:00:00+08:00")
        payments3.dispatch(self.dispatcher, "PAY-0001", expected_version=inst["version"])
        app4, payments4 = self.reopen("2026-09-28T12:00:00+08:00")
        payments4.ingest(self.bridge, "PAY-0001", sequence=1, kind="accepted", occurred_at="2026-09-28T11:05:00+08:00")
        payments4.ingest(self.bridge, "PAY-0001", sequence=2, kind="cleared", occurred_at="2026-09-28T11:10:00+08:00")
        payments4.ingest(self.bridge, "PAY-0001", sequence=3, kind="settled", currency="CNY", amount_minor=1488000, payee_account="ACCT-MO-889", occurred_at="2026-09-28T11:15:00+08:00")
        app5, payments5 = self.reopen("2026-09-28T13:00:00+08:00")
        payments5.ingest(self.bridge, "PAY-0001", sequence=4, kind="refunded", currency="CNY", amount_minor=30000, occurred_at="2026-09-28T12:30:00+08:00")
        # 任一历史时点余额
        self.assertEqual(app5.ledger.account_balance("funds:org:hengqin", "available", currency="CNY", as_of="2026-09-28T10:15:00+08:00"), 5000000)
        self.assertEqual(app5.ledger.account_balance("funds:org:hengqin", "available", currency="CNY", as_of="2026-09-28T11:30:00+08:00"), 3500000)
        self.assertEqual(app5.ledger.account_balance("funds:org:hengqin", "available", currency="CNY", as_of="2026-09-28T13:00:00+08:00"), 3530000)
        self.assertEqual(app5.ledger.account_balance("funds:org:hengqin", "in_transit", currency="CNY", as_of="2026-09-28T11:05:00+08:00"), 1500000)
        self.assertEqual(app5.ledger.account_balance("funds:org:hengqin", "in_transit", currency="CNY", as_of="2026-09-28T12:00:00+08:00"), 0)
        # 统一查询：进度、资金落点、责任人、差错来源、历史时点
        status = payments5.status(self.submitter, "PAY-0001", as_of="2026-09-28T10:45:00+08:00")
        self.assertEqual(status["state"], "settled")
        self.assertEqual([e["kind"] for e in status["progress"]["applied"]], ["accepted", "cleared", "settled", "refunded"])
        self.assertEqual(status["responsible"]["role"], "closed")
        self.assertEqual(status["errors"]["reconciliation_open"], [])
        self.assertEqual(status["as_of"]["state"], "approved")
        self.assertEqual(status["as_of"]["available_balance"], {"CNY": 5000000})
        self.assertEqual(status["as_of"]["fund_location"], {})
        midway = payments5.status(self.submitter, "PAY-0001", as_of="2026-09-28T11:06:00+08:00")
        self.assertEqual(midway["as_of"]["state"], "dispatched")
        self.assertEqual(midway["as_of"]["fund_location"]["CNY"]["in_transit"], 1500000)
        settled_view = payments5.status(self.submitter, "PAY-0001", as_of="2026-09-28T12:30:00+08:00")
        self.assertEqual(settled_view["as_of"]["state"], "settled")
        self.assertEqual(settled_view["as_of"]["fund_location"]["CNY"]["settled_out"], 1488000)

    # ------------------------------------------------------------------
    # 日终关账与恢复
    # ------------------------------------------------------------------

    def test_day_end_recovery_continues_unfinished_steps(self):
        self.drive_to_dispatched()
        self.submit_default(business_key="PAY-0002", contract_id="HT-2026-120")
        # 系统停机期间到达的桥侧事件：只在收件箱等待归并
        self.app.inbox.receive(source="bridge", source_key="PAY-0001", sequence=1, payload={"kind": "accepted"}, occurred_at="2026-09-28T16:00:00+08:00")
        self.app.inbox.receive(source="bridge", source_key="PAY-0001", sequence=2, payload={"kind": "cleared"}, occurred_at="2026-09-28T16:05:00+08:00")
        app1, payments1 = self.reopen("2026-09-28T17:00:00+08:00")
        runner = DayEndClose(app1, payments1)
        runner.schedule(self.closer, "2026-09-28", run_at="2026-09-28T17:00:00+08:00")

        class CrashRunner(DayEndClose):
            def _execute_step(self, run, step, system):
                if step == "snapshot_balances":
                    raise RuntimeError("模拟日终中途停机")
                return super()._execute_step(run, step, system)

        claimed = app1.jobs.claim_due(seconds=1)
        self.assertEqual(len(claimed), 1)
        with self.assertRaises(RuntimeError):
            CrashRunner(app1, payments1)._execute("2026-09-28")
        run = app1.repository.search("day_end_runs", "business_date", "2026-09-28")[0]
        self.assertEqual(run["completed_steps"], ["sync_events", "recheck_profiles", "cutoff_review"])
        self.assertEqual(payments1.get_instruction(self.submitter, "PAY-0001")["state"], "cleared")
        self.assertEqual(payments1.get_instruction(self.submitter, "PAY-0002")["attention"], "carry_over")
        # 租约过期后换个进程恢复：续跑未完成步骤
        app2, payments2 = self.reopen("2026-09-28T17:00:02+08:00")
        results = DayEndClose(app2, payments2).run_due(self.closer)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "closed")
        self.assertEqual(results[0]["completed_steps"], list(STEPS))
        run = app2.repository.search("day_end_runs", "business_date", "2026-09-28")[0]
        self.assertEqual(run["state"], "closed")
        self.assertEqual(len(run["snapshots"]), 1)
        snapshot = app2.repository.get("snapshots", run["snapshots"][0])
        self.assertEqual(snapshot["subject_id"], "funds:org:hengqin")
        self.assertEqual(snapshot["watermark"]["CNY"]["available"], 3500000)
        self.assertEqual(snapshot["watermark"]["CNY"]["in_transit"], 1500000)
        # 恢复后不会重复执行：任务已完成，快照仍只有一份
        self.assertEqual(DayEndClose(app2, payments2).run_due(self.closer), [])
        snapshots = app2.repository.search("snapshots", "subject_id", "funds:org:hengqin")
        self.assertEqual(len(snapshots), 1)
        again = DayEndClose(app2, payments2)._execute("2026-09-28")
        self.assertEqual(again["completed_steps"], list(STEPS))
        self.assertEqual(len(app2.repository.search("snapshots", "subject_id", "funds:org:hengqin")), 1)

    def test_day_end_recheck_holds_changed_profile(self):
        inst = self.submit_default()
        self.payments.approve(self.approver, "PAY-0001", expected_version=inst["version"], reason="复核通过")
        profile = self.payments._current_profile("org:hengqin")
        self.payments.update_profile(self.admin, "org:hengqin", {"account": "ACCT-HQ-002"}, expected_version=profile["version"], request_key="profile-change-3")
        app, payments = self.reopen("2026-09-28T17:00:00+08:00")
        runner = DayEndClose(app, payments)
        runner.schedule(self.closer, "2026-09-28", run_at="2026-09-28T17:00:00+08:00")
        results = runner.run_due(self.closer)
        self.assertEqual(results[0]["status"], "closed")
        inst = payments.get_instruction(self.submitter, "PAY-0001")
        self.assertEqual(inst["state"], "on_hold")
        self.assertEqual(inst["hold_reason"], "profile_changed:payer")
        status = payments.status(self.submitter, "PAY-0001")
        self.assertEqual(status["responsible"]["role"], "compliance_officer")

    def test_crashed_running_job_is_reclaimed_after_lease_expiry(self):
        job = self.app.jobs.schedule(job_type="day_end_close", subject_id="2026-09-28", run_at="2026-09-28T17:00:00+08:00", payload={"business_date": "2026-09-28"})
        app1, _ = self.reopen("2026-09-28T17:00:00+08:00")
        claimed = app1.jobs.claim_due(seconds=1)
        self.assertEqual([item["job_id"] for item in claimed], [job])
        # 持有者崩溃，没有 finish 也没有 retry
        app2, _ = self.reopen("2026-09-28T17:00:01+08:00")
        self.assertEqual(app2.jobs.claim_due(), [])
        app3, _ = self.reopen("2026-09-28T17:00:02+08:00")
        reclaimed = app3.jobs.claim_due()
        self.assertEqual([item["job_id"] for item in reclaimed], [job])


if __name__ == "__main__":
    unittest.main()
