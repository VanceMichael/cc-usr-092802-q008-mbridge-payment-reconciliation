"""跨境付款责任链。

围绕月末关账与差错处理，把平台的授权、审批、不可变分录、事件收件箱和
可恢复任务组合成一条可追责的跨境付款链：

- 提交指令时固化合同验收状态、收付款双方资料和业务水位，并按币种与费用
  形成付款组成；同一业务标识无论重放还是并发提交只得到一笔有效指令。
- 桥侧受理、清算、结算、退回、撤销、退款事件经收件箱按来源序号归并到同一
  经济事项；事件重放或并发到达只会产生一笔有效结果。
- 金额、币种或收款方与既有记录矛盾时进入核对队列，系统不做自动补付。
- 提交者不能批准自己的指令；资料变化或合规命中会暂停后续处理。
- 已结算款项只能通过桥侧退款或冲正留下新的分录，历史分录不可变。
- 查询结果同时给出业务进度、资金落点、当前责任人、差错来源和任一历史时点
  余额；双方岗位只能看到履职范围内的字段。

岗位权限（配合 ``AccessContext`` 使用）：

- ``write:payments`` 提交/撤销指令（另需 ``side:payer`` 范围）
- ``approve:payments`` 批准指令（不能是指令提交者）
- ``dispatch:payments`` 向桥侧发报并冻结资金
- ``ingest:bridge`` 桥侧事件接入与归并
- ``release:payments`` 解除暂停（合规岗位）
- ``resolve:reconciliation`` 处理核对队列
- ``correct:payments`` 冲正
- ``read:payments`` / ``history:payments`` 查询与历史回放
- ``write:profiles`` 维护机构资料
- ``run:dayend`` 执行日终关账

可见范围：``side:payer`` 可见付款方内部字段（业务水位、费用、付款账户），
``side:payee`` 可见收款账户与到账净额，其余字段双方可见。

桥侧事件约定：来源为 ``bridge``，来源标识为业务标识，来源序号从 1 开始
连续编号；归并严格按序号顺序进行，缺号时等待，不乱序应用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .inbox import Inbox
from .jsonutil import canonical_json, digest_json
from .ledger import Ledger, to_minor
from .repository import EntityRepository
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant

INSTRUCTION_TYPE = "payment_instructions"
RECONCILIATION_TYPE = "reconciliation_items"
PROFILE_TYPE = "payment_profiles"

BRIDGE_SOURCE = "bridge"

EVENT_KINDS = ("accepted", "cleared", "settled", "returned", "cancelled", "refunded")

TERMINAL_STATES = ("settled", "returned", "cancelled", "refunded")
PAUSED_STATES = ("on_hold", "reconciling")

ACCEPTANCE_OK = ("验收通过", "accepted")

ACCOUNTS = ("available", "in_transit", "settled_out", "fee_income", "reconciliation_adjustment")

# 状态 -> (责任岗位, 责任方)
RESPONSIBLE = {
    "draft": ("payer_submitter", "payer"),
    "submitted": ("payer_approver", "payer"),
    "approved": ("payer_dispatcher", "payer"),
    "dispatched": ("bridge_operator", "bridge"),
    "accepted": ("bridge_operator", "bridge"),
    "cleared": ("bridge_operator", "bridge"),
    "settling": ("bridge_operator", "bridge"),
    "settled": ("closed", "none"),
    "returned": ("closed", "none"),
    "cancelled": ("closed", "none"),
    "refunded": ("closed", "none"),
    "on_hold": ("compliance_officer", "payer"),
    "reconciling": ("reconciler", "payer"),
}


class _Anomaly(Exception):
    """桥侧事件与既有记录矛盾，需要进入核对队列。"""

    def __init__(self, category: str, reason: str, expected: object = None, actual: object = None):
        super().__init__(reason)
        self.category = category
        self.reason = reason
        self.expected = expected
        self.actual = actual


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()


def _build_components(items: Iterable[Mapping[str, object]]) -> tuple[list[dict], dict]:
    """把费用明细行归并为按币种的付款组成。"""
    lines: list[dict] = []
    for index, item in enumerate(items, 1):
        currency = require_safe(str(item.get("currency", "")), "币种")
        amount = to_minor(str(item.get("amount", "")))
        fee = to_minor(str(item.get("fee", "0")))
        if amount <= 0:
            raise ValidationError("金额必须大于零")
        if fee < 0 or fee >= amount:
            raise ValidationError("费用必须不为负且小于金额")
        purpose = str(item.get("purpose", "")).strip() or "服务费"
        lines.append({"line": index, "currency": currency, "amount_minor": amount, "fee_minor": fee, "purpose": purpose})
    if not lines:
        raise ValidationError("付款组成不能为空")
    composition: dict[str, dict] = {}
    for line in lines:
        slot = composition.setdefault(line["currency"], {"gross_minor": 0, "fee_minor": 0, "net_minor": 0})
        slot["gross_minor"] += line["amount_minor"]
        slot["fee_minor"] += line["fee_minor"]
        slot["net_minor"] += line["amount_minor"] - line["fee_minor"]
    return lines, composition


def _profile_digest(profile: Mapping[str, object]) -> str:
    return digest_json({"org_id": profile["org_id"], "name": profile["name"], "account": profile["account"], "jurisdiction": profile["jurisdiction"]})


@dataclass(frozen=True)
class CrossBorderPayments:
    """跨境付款责任链服务：指令、审批、事件归并、核对、退款与冲正。"""

    database: Database
    repository: EntityRepository
    inbox: Inbox
    ledger: Ledger
    clock: Clock
    sanctioned: frozenset[str] = frozenset()

    @classmethod
    def open(cls, app, *, sanctioned: Iterable[str] = ()) -> "CrossBorderPayments":
        return cls(app.database, app.repository, app.inbox, app.ledger, app.clock, frozenset(sanctioned))

    # ------------------------------------------------------------------
    # 机构资料
    # ------------------------------------------------------------------

    def register_profile(self, context: AccessContext, *, org_id: str, name: str, account: str, jurisdiction: str, request_key: str | None = None) -> dict:
        context.require("write:profiles")
        require_safe(org_id, "机构标识")
        if self.repository.search(PROFILE_TYPE, "org_id", org_id):
            raise ConflictError("机构资料已存在，请使用 update_profile 变更")
        payload = {"org_id": org_id, "name": _require_text(name, "机构名称"), "account": _require_text(account, "机构账户"), "jurisdiction": _require_text(jurisdiction, "辖区"), "state": "effective"}
        return self.repository.create(PROFILE_TYPE, payload, actor=context.actor_id, request_key=request_key or f"profile:{org_id}")

    def update_profile(self, context: AccessContext, org_id: str, changes: Mapping[str, object], *, expected_version: int, request_key: str) -> dict:
        context.require("write:profiles")
        profile = self._current_profile(org_id)
        unknown = set(changes) - {"name", "account", "jurisdiction"}
        if unknown:
            raise ValidationError("未知资料字段: " + ", ".join(sorted(unknown)))
        clean = {key: _require_text(value, key) for key, value in changes.items()}
        return self.repository.update(PROFILE_TYPE, profile["entity_id"], clean, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def _current_profile(self, org_id: str) -> dict:
        rows = self.repository.search(PROFILE_TYPE, "org_id", org_id)
        if not rows:
            raise NotFoundError(f"机构 {org_id} 的资料不存在")
        return rows[0]

    # ------------------------------------------------------------------
    # 指令提交（固化验收状态、双方资料、业务水位与付款组成）
    # ------------------------------------------------------------------

    def submit(self, context: AccessContext, *, business_key: str, contract_id: str, acceptance_status: str, payer_org: str, payee_org: str, components: Iterable[Mapping[str, object]], memo: str = "", request_key: str | None = None) -> dict:
        context.require("write:payments")
        self._require_side(context, "side:payer")
        require_safe(business_key, "业务标识")
        require_safe(contract_id, "合同标识")
        require_safe(payer_org, "付款机构")
        require_safe(payee_org, "收款机构")
        acceptance_status = _require_text(acceptance_status, "合同验收状态")
        if acceptance_status not in ACCEPTANCE_OK:
            raise ValidationError("合同未验收通过，不得提交付款")
        lines, composition = _build_components(components)
        payer = self._current_profile(payer_org)
        payee = self._current_profile(payee_org)
        journal = self._journal_for(payer_org)
        water_level = {currency: self.ledger.account_balance(journal, "available", currency=currency) for currency in composition}
        for currency, slot in composition.items():
            if water_level[currency] < slot["gross_minor"]:
                raise ValidationError(f"{currency} 业务水位不足：需要 {slot['gross_minor']}，当前 {water_level[currency]}")
        frozen_payer = {key: payer[key] for key in ("org_id", "name", "account", "jurisdiction")}
        frozen_payee = {key: payee[key] for key in ("org_id", "name", "account", "jurisdiction")}
        payload = {
            "business_key": business_key,
            "contract_id": contract_id,
            "acceptance_status": acceptance_status,
            "payer_org": payer_org,
            "payee_org": payee_org,
            "payer_profile": frozen_payer,
            "payee_profile": frozen_payee,
            "profile_digest": {"payer": _profile_digest(frozen_payer), "payee": _profile_digest(frozen_payee)},
            "profile_refreshes": [],
            "water_level": water_level,
            "components": lines,
            "composition": composition,
            "memo": memo.strip(),
            "submitted_by": context.actor_id,
            "submitted_at": self.clock.now(),
            "approved_by": "",
            "approved_at": "",
            "approval_reason": "",
            "applied_sequences": [],
            "event_trail": [],
            "settled": {},
            "refunded": {},
            "corrections": [],
            "hold_reason": "",
            "held_from": "",
            "reconcile_from": "",
            "error_note": "",
            "attention": "",
        }
        payload["submit_digest"] = digest_json({"business_key": business_key, "contract_id": contract_id, "payer_org": payer_org, "payee_org": payee_org, "components": lines, "memo": payload["memo"]})
        hit = self._compliance_hit(frozen_payer, frozen_payee)
        if hit:
            payload.update({"state": "on_hold", "held_from": "submitted", "hold_reason": "compliance:" + ",".join(hit)})
        else:
            payload["state"] = "submitted"
        return self._create_instruction(payload, actor=context.actor_id, request_key=request_key or f"submit:{business_key}")

    def _create_instruction(self, payload: dict, *, actor: str, request_key: str) -> dict:
        """在单个事务内完成业务标识唯一性校验、建账与审计，并发提交只产生一笔有效指令。"""
        with self.database.transaction() as connection:
            def operation() -> dict:
                for row in connection.execute("SELECT * FROM entities WHERE entity_type=?", (INSTRUCTION_TYPE,)):
                    existing = json.loads(row["payload_json"])
                    if existing.get("business_key") == payload["business_key"]:
                        if existing.get("submit_digest") != payload["submit_digest"]:
                            raise ConflictError("同一业务标识对应不同的付款内容")
                        return EntityRepository._row_to_dict(row)
                entity_id = new_id("payment")
                now = self.clock.now()
                connection.execute("INSERT INTO entities(entity_type,entity_id,version,state,payload_json,created_at,updated_at,created_by,updated_by) VALUES(?,?,?,?,?,?,?,?,?)", (INSTRUCTION_TYPE, entity_id, 1, payload["state"], canonical_json(payload), now, now, actor, actor))
                connection.execute("INSERT INTO entity_versions(entity_type,entity_id,version,state,payload_json,valid_from,actor_id,request_key) VALUES(?,?,?,?,?,?,?,?)", (INSTRUCTION_TYPE, entity_id, 1, payload["state"], canonical_json(payload), now, actor, request_key))
                self.repository.audit.append(connection, actor_id=actor, action="create", entity_type=INSTRUCTION_TYPE, entity_id=entity_id, version=1, detail=payload)
                return EntityRepository._row_to_dict(connection.execute("SELECT * FROM entities WHERE entity_type=? AND entity_id=?", (INSTRUCTION_TYPE, entity_id)).fetchone())
            return self.repository.idempotency.execute(connection, scope="payment:submit", request_key=request_key, request={"business_key": payload["business_key"], "submit_digest": payload["submit_digest"]}, operation=operation)

    # ------------------------------------------------------------------
    # 审批、发报、撤销（职责分离：提交者不能批准）
    # ------------------------------------------------------------------

    def approve(self, context: AccessContext, business_key: str, *, expected_version: int, reason: str, request_key: str | None = None) -> dict:
        context.require("approve:payments")
        reason = _require_text(reason, "审批意见")
        inst = self._get(business_key)
        if inst["state"] == "approved" and inst["approved_by"] == context.actor_id:
            return inst
        if inst["state"] != "submitted":
            raise ConflictError(f"当前状态 {inst['state']} 不允许批准")
        assert_distinct(inst["submitted_by"], context.actor_id)
        hit = self._compliance_hit(self._current_profile(inst["payer_org"]), self._current_profile(inst["payee_org"]))
        if hit:
            changes = {"state": "on_hold", "held_from": "submitted", "hold_reason": "compliance:" + ",".join(hit)}
        else:
            changes = {"state": "approved", "approved_by": context.actor_id, "approved_at": self.clock.now(), "approval_reason": reason}
        return self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key or f"approve:{business_key}:{context.actor_id}:v{expected_version}")

    def dispatch(self, context: AccessContext, business_key: str, *, expected_version: int, request_key: str | None = None) -> dict:
        context.require("dispatch:payments")
        inst = self._get(business_key)
        if inst["state"] == "dispatched":
            return inst
        if inst["state"] != "approved":
            raise ConflictError(f"当前状态 {inst['state']} 不允许发报")
        hold = self._recheck_reason(inst)
        if hold:
            return self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], {"state": "on_hold", "held_from": "approved", "hold_reason": hold}, actor=context.actor_id, expected_version=expected_version, request_key=request_key or f"dispatch-hold:{business_key}:v{expected_version}")
        journal = self._journal(inst)
        for currency, slot in inst["composition"].items():
            self.ledger.post_if_absent(journal_key=journal, account="in_transit", currency=currency, amount_minor=slot["gross_minor"], direction="debit", reference=f"{business_key}:dispatch:{currency}:in", actor=context.actor_id)
            self.ledger.post_if_absent(journal_key=journal, account="available", currency=currency, amount_minor=slot["gross_minor"], direction="credit", reference=f"{business_key}:dispatch:{currency}:out", actor=context.actor_id)
        return self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], {"state": "dispatched", "dispatched_at": self.clock.now()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key or f"dispatch:{business_key}:v{expected_version}")

    def cancel(self, context: AccessContext, business_key: str, *, expected_version: int, reason: str, request_key: str | None = None) -> dict:
        context.require("write:payments")
        self._require_side(context, "side:payer")
        reason = _require_text(reason, "撤销原因")
        inst = self._get(business_key)
        if inst["state"] == "cancelled":
            return inst
        if inst["state"] not in ("approved", "dispatched", "accepted"):
            raise ConflictError(f"当前状态 {inst['state']} 不允许撤销")
        changes = self._cancel_changes(inst, actor=context.actor_id)
        changes["cancel_reason"] = reason
        return self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key or f"cancel:{business_key}:v{expected_version}")

    def _cancel_changes(self, inst: dict, *, actor: str) -> dict:
        changes = {"state": "cancelled"}
        if inst["state"] in ("dispatched", "accepted"):
            journal = self._journal(inst)
            for currency, slot in inst["composition"].items():
                if currency in inst["settled"]:
                    continue
                self.ledger.post_if_absent(journal_key=journal, account="available", currency=currency, amount_minor=slot["gross_minor"], direction="debit", reference=f"{inst['business_key']}:cancelled:{currency}:in", actor=actor)
                self.ledger.post_if_absent(journal_key=journal, account="in_transit", currency=currency, amount_minor=slot["gross_minor"], direction="credit", reference=f"{inst['business_key']}:cancelled:{currency}:out", actor=actor)
        return changes

    # ------------------------------------------------------------------
    # 暂停与恢复（资料变化或合规命中暂停后续处理）
    # ------------------------------------------------------------------

    def hold(self, context: AccessContext, business_key: str, *, expected_version: int, reason: str, request_key: str | None = None) -> dict:
        context.require("write:payments")
        reason = _require_text(reason, "暂停原因")
        inst = self._get(business_key)
        if inst["state"] in TERMINAL_STATES or inst["state"] in PAUSED_STATES:
            raise ConflictError(f"当前状态 {inst['state']} 不允许暂停")
        return self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], {"state": "on_hold", "held_from": inst["state"], "hold_reason": reason}, actor=context.actor_id, expected_version=expected_version, request_key=request_key or f"hold:{business_key}:{context.actor_id}:v{expected_version}")

    def resume(self, context: AccessContext, business_key: str, *, expected_version: int, override_reason: str = "", request_key: str | None = None) -> dict:
        """解除暂停；以当前机构资料重新固化并留痕，合规命中未消除时须说明 override 理由。"""
        context.require("release:payments")
        inst = self._get(business_key)
        if inst["state"] != "on_hold":
            raise ConflictError(f"当前状态 {inst['state']} 不在暂停中")
        target = inst["held_from"] or "submitted"
        payer = self._current_profile(inst["payer_org"])
        payee = self._current_profile(inst["payee_org"])
        hit = self._compliance_hit(payer, payee)
        if hit and not override_reason.strip():
            raise ConflictError("合规命中未消除: " + ",".join(hit))
        frozen_payer = {key: payer[key] for key in ("org_id", "name", "account", "jurisdiction")}
        frozen_payee = {key: payee[key] for key in ("org_id", "name", "account", "jurisdiction")}
        refreshes = list(inst["profile_refreshes"])
        new_digest = {"payer": _profile_digest(frozen_payer), "payee": _profile_digest(frozen_payee)}
        if new_digest != inst["profile_digest"]:
            refreshes.append({"at": self.clock.now(), "by": context.actor_id, "previous": inst["profile_digest"], "current": new_digest})
        changes = {"state": target, "held_from": "", "hold_reason": "", "payer_profile": frozen_payer, "payee_profile": frozen_payee, "profile_digest": new_digest, "profile_refreshes": refreshes}
        if override_reason.strip():
            changes["override_reason"] = override_reason.strip()
        return self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key or f"resume:{business_key}:{context.actor_id}:v{expected_version}")

    def recheck(self, context: AccessContext, business_key: str, *, request_key: str | None = None) -> dict:
        """复核机构资料与合规名单，资料变化或命中即暂停后续处理（日终使用）。"""
        if not (context.allows("run:dayend") or context.allows("release:payments")):
            raise PermissionDenied("缺少权限: run:dayend")
        inst = self._get(business_key)
        if inst["state"] in TERMINAL_STATES or inst["state"] in PAUSED_STATES:
            return inst
        hold = self._recheck_reason(inst)
        if not hold:
            return inst
        return self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], {"state": "on_hold", "held_from": inst["state"], "hold_reason": hold}, actor=context.actor_id, expected_version=inst["version"], request_key=request_key or f"recheck-hold:{business_key}:v{inst['version']}")

    def _recheck_reason(self, inst: dict) -> str:
        for side in ("payer", "payee"):
            current = self._current_profile(inst[f"{side}_org"])
            if _profile_digest(current) != inst["profile_digest"][side]:
                return f"profile_changed:{side}"
        hit = self._compliance_hit(self._current_profile(inst["payer_org"]), self._current_profile(inst["payee_org"]))
        if hit:
            return "compliance:" + ",".join(hit)
        return ""

    def _compliance_hit(self, payer: Mapping[str, object], payee: Mapping[str, object]) -> list[str]:
        if not self.sanctioned:
            return []
        tokens = {str(payer["org_id"]), str(payer["account"]), str(payer["name"]), str(payee["org_id"]), str(payee["account"]), str(payee["name"])}
        return sorted(tokens & self.sanctioned)

    # ------------------------------------------------------------------
    # 桥侧事件接入与按来源序号归并
    # ------------------------------------------------------------------

    def ingest(self, context: AccessContext, business_key: str, *, sequence: int, kind: str, occurred_at: str, currency: str | None = None, amount_minor: int | None = None, payee_account: str | None = None) -> dict:
        context.require("ingest:bridge")
        self._get(business_key)
        if kind not in EVENT_KINDS:
            raise ValidationError(f"未知桥侧事件类型: {kind}")
        payload: dict[str, object] = {"kind": kind}
        if currency is not None:
            payload["currency"] = require_safe(currency, "币种")
        if amount_minor is not None:
            if not isinstance(amount_minor, int) or amount_minor <= 0:
                raise ValidationError("金额必须为正整数最小单位")
            payload["amount_minor"] = amount_minor
        if payee_account is not None:
            payload["payee_account"] = _require_text(payee_account, "收款账户")
        receipt = self.inbox.receive(source=BRIDGE_SOURCE, source_key=business_key, sequence=sequence, payload=payload, occurred_at=occurred_at)
        return {"inbox": receipt, "fold": self._fold_pending(business_key)}

    def sync(self, context: AccessContext, business_key: str) -> dict:
        if not (context.allows("ingest:bridge") or context.allows("run:dayend")):
            raise PermissionDenied("缺少权限: ingest:bridge")
        self._get(business_key)
        return self._fold_pending(business_key)

    def _fold_pending(self, business_key: str) -> dict:
        """把已接收事件按来源序号顺序归并到指令；重放与并发只会得到一笔有效结果。"""
        applied_now: list[int] = []
        for _ in range(64):
            inst = self._get(business_key)
            if inst["state"] in PAUSED_STATES:
                break
            applied = set(inst["applied_sequences"])
            messages = [m for m in self.inbox.timeline(BRIDGE_SOURCE, business_key) if m["status"] == "accepted"]
            pending = sorted((m for m in messages if m["sequence"] not in applied), key=lambda m: m["sequence"])
            if not pending:
                break
            nxt = pending[0]
            expected_seq = (max(applied) + 1) if applied else 1
            if nxt["sequence"] != expected_seq:
                break  # 缺号：等待先到的序号，不乱序归并
            try:
                self._apply_event(inst, nxt)
            except ConflictError:
                continue  # 并发归并者已推进，重新读取继续
            applied_now.append(nxt["sequence"])
        inst = self._get(business_key)
        remaining = [m["sequence"] for m in self.inbox.timeline(BRIDGE_SOURCE, business_key) if m["status"] == "accepted" and m["sequence"] not in set(inst["applied_sequences"])]
        return {"applied": applied_now, "state": inst["state"], "pending": remaining}

    def _apply_event(self, inst: dict, message: dict) -> None:
        payload = json.loads(message["payload_json"])
        kind = payload["kind"]
        try:
            if kind == "accepted":
                changes = self._fold_progress(inst, "dispatched", "accepted")
            elif kind == "cleared":
                changes = self._fold_progress(inst, "accepted", "cleared")
            elif kind == "settled":
                changes = self._fold_settled(inst, payload)
            elif kind == "returned":
                changes = self._fold_returned(inst, payload)
            elif kind == "cancelled":
                changes = self._fold_cancelled(inst, payload)
            elif kind == "refunded":
                changes = self._fold_refund(inst, payload, message["sequence"])
            else:
                raise ValidationError(f"未知桥侧事件类型: {kind}")
        except _Anomaly as anomaly:
            self._fold_anomaly(inst, message, anomaly)
            return
        note = changes.pop("note", "")
        business_key = inst["business_key"]
        changes["applied_sequences"] = sorted(set(inst["applied_sequences"]) | {message["sequence"]})
        changes["event_trail"] = list(inst["event_trail"]) + [{"sequence": message["sequence"], "kind": kind, "occurred_at": message["occurred_at"], "note": note}]
        self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], changes, actor="bridge", expected_version=inst["version"], request_key=f"fold:{business_key}:{message['sequence']}:v{inst['version']}")

    def _fold_progress(self, inst: dict, required: str, target: str) -> dict:
        state = inst["state"]
        if state == required:
            return {"state": target}
        if state == target:
            return {"note": "late_duplicate"}
        raise _Anomaly("state_anomaly", f"状态 {state} 不应收到 {target} 事件", expected=required, actual=state)

    def _fold_settled(self, inst: dict, payload: Mapping[str, object]) -> dict:
        state = inst["state"]
        business_key = inst["business_key"]
        currency = payload.get("currency")
        amount = payload.get("amount_minor")
        payee_account = str(payload.get("payee_account", ""))
        if not currency or not isinstance(amount, int) or amount <= 0:
            raise _Anomaly("state_anomaly", "结算回执缺少币种或金额", expected="currency+amount_minor", actual=payload)
        composition = inst["composition"]
        settled = dict(inst["settled"])
        if state in ("returned", "cancelled"):
            raise _Anomaly("state_anomaly", f"状态 {state} 之后收到结算回执", expected=state, actual="settled")
        if currency not in composition:
            raise _Anomaly("currency_mismatch", "回执币种不在付款组成中", expected=sorted(composition), actual=currency)
        if currency in settled:
            prior = settled[currency]
            if prior["net_minor"] == amount and prior["payee_account"] == payee_account:
                return {"note": "late_duplicate"}
            category = "payee_mismatch" if prior["payee_account"] != payee_account else "amount_mismatch"
            raise _Anomaly(category, "回执与既有结算记录矛盾", expected={"currency": currency, "net_minor": prior["net_minor"], "payee_account": prior["payee_account"]}, actual={"currency": currency, "net_minor": amount, "payee_account": payee_account})
        if state not in ("cleared", "settling"):
            raise _Anomaly("state_anomaly", f"状态 {state} 不应收到结算回执", expected="cleared", actual=state)
        expected = composition[currency]
        if payee_account != inst["payee_profile"]["account"]:
            raise _Anomaly("payee_mismatch", "回执收款方与指令不符", expected=inst["payee_profile"]["account"], actual=payee_account)
        if amount != expected["net_minor"]:
            raise _Anomaly("amount_mismatch", "回执金额与付款组成不符", expected={"currency": currency, "net_minor": expected["net_minor"]}, actual={"currency": currency, "net_minor": amount})
        journal = self._journal(inst)
        self.ledger.post_if_absent(journal_key=journal, account="in_transit", currency=currency, amount_minor=expected["gross_minor"], direction="credit", reference=f"{business_key}:settled:{currency}:out", actor="bridge")
        self.ledger.post_if_absent(journal_key=journal, account="settled_out", currency=currency, amount_minor=expected["net_minor"], direction="debit", reference=f"{business_key}:settled:{currency}:net", actor="bridge")
        if expected["fee_minor"]:
            self.ledger.post_if_absent(journal_key=journal, account="fee_income", currency=currency, amount_minor=expected["fee_minor"], direction="debit", reference=f"{business_key}:settled:{currency}:fee", actor="bridge")
        settled[currency] = {"gross_minor": expected["gross_minor"], "fee_minor": expected["fee_minor"], "net_minor": expected["net_minor"], "payee_account": payee_account, "at": self.clock.now()}
        new_state = "settled" if all(c in settled for c in composition) else "settling"
        return {"state": new_state, "settled": settled}

    def _fold_returned(self, inst: dict, payload: Mapping[str, object]) -> dict:
        state = inst["state"]
        if state == "returned":
            return {"note": "late_duplicate"}
        if state in ("settled", "refunded"):
            raise _Anomaly("state_anomaly", f"状态 {state} 不应收到退回", expected="dispatched", actual=state)
        if state not in ("dispatched", "accepted", "cleared", "settling"):
            raise _Anomaly("state_anomaly", f"状态 {state} 不应收到退回", expected="dispatched", actual=state)
        journal = self._journal(inst)
        business_key = inst["business_key"]
        for currency, slot in inst["composition"].items():
            if currency in inst["settled"]:
                continue
            self.ledger.post_if_absent(journal_key=journal, account="available", currency=currency, amount_minor=slot["gross_minor"], direction="debit", reference=f"{business_key}:returned:{currency}:in", actor="bridge")
            self.ledger.post_if_absent(journal_key=journal, account="in_transit", currency=currency, amount_minor=slot["gross_minor"], direction="credit", reference=f"{business_key}:returned:{currency}:out", actor="bridge")
        return {"state": "returned"}

    def _fold_cancelled(self, inst: dict, payload: Mapping[str, object]) -> dict:
        state = inst["state"]
        if state == "cancelled":
            return {"note": "late_duplicate"}
        if state not in ("approved", "dispatched", "accepted"):
            raise _Anomaly("state_anomaly", f"状态 {state} 不应收到撤销", expected="approved", actual=state)
        return self._cancel_changes(inst, actor="bridge")

    def _fold_refund(self, inst: dict, payload: Mapping[str, object], sequence: int) -> dict:
        state = inst["state"]
        currency = payload.get("currency")
        amount = payload.get("amount_minor")
        if not currency or not isinstance(amount, int) or amount <= 0:
            raise _Anomaly("state_anomaly", "退款回执缺少币种或金额", expected="currency+amount_minor", actual=payload)
        settled = inst["settled"]
        if currency not in settled:
            raise _Anomaly("state_anomaly", "未结算币种收到退款", expected=sorted(settled), actual=currency)
        if state not in ("settled", "returned", "refunded"):
            raise _Anomaly("state_anomaly", f"状态 {state} 不应收到退款", expected="settled", actual=state)
        refunded = dict(inst["refunded"])
        done = refunded.get(currency, 0)
        remaining = settled[currency]["net_minor"] - done
        if amount > remaining:
            raise _Anomaly("over_refund", "退款超过可退余额", expected={"currency": currency, "remaining_minor": remaining}, actual={"currency": currency, "amount_minor": amount})
        journal = self._journal(inst)
        business_key = inst["business_key"]
        self.ledger.post_if_absent(journal_key=journal, account="available", currency=currency, amount_minor=amount, direction="debit", reference=f"{business_key}:refund:{currency}:{sequence}:in", actor="bridge")
        self.ledger.post_if_absent(journal_key=journal, account="settled_out", currency=currency, amount_minor=amount, direction="credit", reference=f"{business_key}:refund:{currency}:{sequence}:out", actor="bridge")
        refunded[currency] = done + amount
        changes: dict = {"refunded": refunded}
        if all(refunded.get(c, 0) >= s["net_minor"] for c, s in settled.items()):
            changes["state"] = "refunded"
        return changes

    def _fold_anomaly(self, inst: dict, message: dict, anomaly: _Anomaly) -> None:
        """矛盾事件进入核对队列：只记录、不动资金、不自动补付。"""
        business_key = inst["business_key"]
        sequence = message["sequence"]
        payload = json.loads(message["payload_json"])
        existing = [item for item in self.repository.search(RECONCILIATION_TYPE, "business_key", business_key, limit=500) if item["sequence"] == sequence]
        if existing and existing[0]["state"] == "resolved":
            # 核对项在事件重归并前已被人工处理：按处理结果落账，不再进入核对状态
            item = existing[0]
            changes: dict = {}
            if item["resolution"] == "accept_incoming":
                changes.update(self._accept_incoming(inst, item))
            changes["applied_sequences"] = sorted(set(inst["applied_sequences"]) | {sequence})
            changes["event_trail"] = list(inst["event_trail"]) + [{"sequence": sequence, "kind": payload["kind"], "occurred_at": message["occurred_at"], "note": f"anomaly_resolved:{item['resolution']}"}]
            self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], changes, actor="bridge-monitor", expected_version=inst["version"], request_key=f"fold:{business_key}:{sequence}:v{inst['version']}")
            return
        self.repository.create(RECONCILIATION_TYPE, {
            "business_key": business_key,
            "sequence": sequence,
            "category": anomaly.category,
            "reason": anomaly.reason,
            "expected": anomaly.expected,
            "actual": anomaly.actual,
            "state": "open",
            "opened_at": self.clock.now(),
            "opened_by": "bridge-monitor",
            "resolution": "",
            "resolved_by": "",
            "resolved_at": "",
        }, actor="bridge-monitor", request_key=f"recon:{business_key}:{sequence}")
        changes = {
            "state": "reconciling",
            "reconcile_from": inst["state"] if inst["state"] != "reconciling" else inst["reconcile_from"],
            "error_note": anomaly.reason,
            "applied_sequences": sorted(set(inst["applied_sequences"]) | {sequence}),
            "event_trail": list(inst["event_trail"]) + [{"sequence": sequence, "kind": payload["kind"], "occurred_at": message["occurred_at"], "note": f"anomaly:{anomaly.category}"}],
        }
        self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], changes, actor="bridge-monitor", expected_version=inst["version"], request_key=f"fold:{business_key}:{sequence}:v{inst['version']}")

    # ------------------------------------------------------------------
    # 核对队列处理（人工确认，绝不自动补付）
    # ------------------------------------------------------------------

    def reconciliation_queue(self, context: AccessContext, *, state: str = "open") -> list[dict]:
        context.require("read:payments")
        if state not in ("open", "resolved"):
            raise ValidationError("未知核对状态")
        return self.repository.list(RECONCILIATION_TYPE, state=state, limit=500)

    def resolve_reconciliation(self, context: AccessContext, item_id: str, *, action: str, reason: str, request_key: str | None = None) -> dict:
        context.require("resolve:reconciliation")
        reason = _require_text(reason, "处理意见")
        if action not in ("confirm_existing", "accept_incoming"):
            raise ValidationError("处理方式必须是 confirm_existing 或 accept_incoming")
        item = self.repository.get(RECONCILIATION_TYPE, item_id)
        if item["state"] == "resolved":
            if item["resolution"] == action:
                return {"item": item, "replayed": True}
            raise ConflictError("核对项已按其他方式处理")
        inst = self._get(item["business_key"])
        business_key = inst["business_key"]
        inst_changes: dict = {}
        if action == "accept_incoming":
            inst_changes = self._accept_incoming(inst, item)
        if inst["state"] == "reconciling":
            restored = inst_changes.get("state") or inst["reconcile_from"] or "submitted"
            inst_changes.setdefault("state", restored)
            inst_changes.update({"reconcile_from": "", "error_note": ""})
        if inst_changes:
            self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], inst_changes, actor=context.actor_id, expected_version=inst["version"], request_key=f"resolve-inst:{item_id}")
        resolved = self.repository.update(RECONCILIATION_TYPE, item_id, {"state": "resolved", "resolution": action, "resolution_reason": reason, "resolved_by": context.actor_id, "resolved_at": self.clock.now()}, actor=context.actor_id, expected_version=item["version"], request_key=request_key or f"resolve-item:{item_id}")
        fold = self._fold_pending(business_key)
        return {"item": resolved, "replayed": False, "fold": fold}

    def _accept_incoming(self, inst: dict, item: dict) -> dict:
        """以桥侧来电为准补记差额分录（人工确认后的显式动作，不是自动补付）。"""
        category = item["category"]
        business_key = inst["business_key"]
        journal = self._journal(inst)
        settled = dict(inst["settled"])
        actual = item["actual"] or {}
        currency = actual.get("currency") if isinstance(actual, Mapping) else None
        if category == "amount_mismatch" and currency and currency in inst["composition"]:
            target = int(actual["net_minor"])
            if target <= 0:
                raise ValidationError("来电金额不合法")
            sequence = item["sequence"]
            current = settled.get(currency)
            if current and current.get("adjusted") and current["net_minor"] == target:
                return {}  # 已应用过来电为准，重放直接跳过
            if currency in settled:
                posted = settled[currency]["net_minor"]
                delta = target - posted
                if delta:
                    direction = "debit" if delta > 0 else "credit"
                    contra = "credit" if delta > 0 else "debit"
                    self.ledger.post_if_absent(journal_key=journal, account="settled_out", currency=currency, amount_minor=abs(delta), direction=direction, reference=f"{business_key}:adjust:{sequence}:{currency}:settled", actor="reconciler")
                    self.ledger.post_if_absent(journal_key=journal, account="reconciliation_adjustment", currency=currency, amount_minor=abs(delta), direction=contra, reference=f"{business_key}:adjust:{sequence}:{currency}:contra", actor="reconciler")
                settled[currency] = {**settled[currency], "net_minor": target, "adjusted": True}
            else:
                gross = inst["composition"][currency]["gross_minor"]
                self.ledger.post_if_absent(journal_key=journal, account="in_transit", currency=currency, amount_minor=gross, direction="credit", reference=f"{business_key}:settled:{currency}:out", actor="reconciler")
                self.ledger.post_if_absent(journal_key=journal, account="settled_out", currency=currency, amount_minor=target, direction="debit", reference=f"{business_key}:settled:{currency}:net", actor="reconciler")
                balance = gross - target
                if balance:
                    direction = "debit" if balance > 0 else "credit"
                    self.ledger.post_if_absent(journal_key=journal, account="reconciliation_adjustment", currency=currency, amount_minor=abs(balance), direction=direction, reference=f"{business_key}:settled:{currency}:adjust", actor="reconciler")
                settled[currency] = {"gross_minor": gross, "fee_minor": 0, "net_minor": target, "payee_account": str(actual.get("payee_account", inst["payee_profile"]["account"])), "at": self.clock.now(), "adjusted": True}
            changes: dict = {"settled": settled}
            if all(c in settled for c in inst["composition"]):
                changes["state"] = "settled"
            return changes
        if category == "payee_mismatch" and currency and currency in settled:
            if settled[currency].get("adjusted") and settled[currency]["payee_account"] == str(actual.get("payee_account", "")):
                return {}  # 已应用，重放跳过
            settled[currency] = {**settled[currency], "payee_account": str(actual.get("payee_account", "")), "adjusted": True}
            return {"settled": settled}
        raise ValidationError(f"类别 {category} 不支持以来电为准，只能维持原记录")

    # ------------------------------------------------------------------
    # 冲正（已结算款项只能留下新的分录）
    # ------------------------------------------------------------------

    def correct(self, context: AccessContext, business_key: str, *, entry_id: str, reason: str, request_key: str | None = None) -> dict:
        context.require("correct:payments")
        reason = _require_text(reason, "冲正原因")
        inst = self._get(business_key)
        if inst["state"] not in ("settled", "refunded", "reconciling", "returned"):
            raise ConflictError("仅结算或退回后的款项可以冲正")
        entry = next((e for e in self.ledger.entries(self._journal(inst)) if e["entry_id"] == entry_id), None)
        if not entry or not entry["reference"].startswith(f"{business_key}:"):
            raise NotFoundError("分录不属于该付款")
        reversal = self.ledger.reverse(entry_id, reference=f"{business_key}:reversal:{entry_id}", actor=context.actor_id)
        corrections = list(inst["corrections"])
        if not any(c["entry_id"] == entry_id for c in corrections):
            corrections.append({"entry_id": entry_id, "reversal_entry_id": reversal["entry_id"], "reason": reason, "at": self.clock.now(), "by": context.actor_id})
            inst = self.repository.update(INSTRUCTION_TYPE, inst["entity_id"], {"corrections": corrections}, actor=context.actor_id, expected_version=inst["version"], request_key=request_key or f"correct:{business_key}:{entry_id}")
        return {"reversal": reversal, "instruction": inst}

    # ------------------------------------------------------------------
    # 统一查询：业务进度、资金落点、当前责任人、差错来源、历史时点余额
    # ------------------------------------------------------------------

    def get_instruction(self, context: AccessContext, business_key: str) -> dict:
        context.require("read:payments")
        return self._redact(self._get(business_key), context)

    def list_instructions(self, context: AccessContext, *, state: str | None = None, limit: int = 100) -> list[dict]:
        context.require("read:payments")
        return [self._redact(row, context) for row in self.repository.list(INSTRUCTION_TYPE, state=state, limit=limit)]

    def status(self, context: AccessContext, business_key: str, *, as_of: str | None = None) -> dict:
        context.require("read:payments")
        inst = self._get(business_key)
        role, side = RESPONSIBLE.get(inst["state"], ("unknown", "unknown"))
        payer_scope = context.has_scope("side:payer")
        pending = [m["sequence"] for m in self.inbox.timeline(BRIDGE_SOURCE, business_key) if m["status"] == "accepted" and m["sequence"] not in set(inst["applied_sequences"])]
        open_items = [self._redact_item(item, context) for item in self.repository.search(RECONCILIATION_TYPE, "business_key", business_key, limit=500) if item["state"] == "open"]
        view = {
            "business_key": business_key,
            "state": inst["state"],
            "submitted_by": inst["submitted_by"],
            "submitted_at": inst["submitted_at"],
            "approved_by": inst["approved_by"],
            "approved_at": inst["approved_at"],
            "progress": {"applied": inst["event_trail"], "pending_sequences": pending},
            "fund_location": self._fund_location(inst) if payer_scope else "***",
            "settlement_summary": self._settlement_summary(inst, payer_scope),
            "responsible": {"role": role, "side": side, "last_actor": inst["updated_by"], "detail": inst["hold_reason"] or inst["error_note"]},
            "errors": {
                "reconciliation_open": open_items,
                "inbox_conflicts": self.inbox.conflicts(BRIDGE_SOURCE, business_key),
                "hold_reason": inst["hold_reason"],
                "error_note": inst["error_note"],
            },
            "corrections": inst["corrections"],
            "instruction": self._redact(inst, context),
        }
        if as_of is not None:
            instant = canonical_instant(as_of)
            try:
                state_at = self.repository.snapshot(INSTRUCTION_TYPE, inst["entity_id"], as_of=instant)["state"]
            except NotFoundError:
                state_at = None
            view["as_of"] = {
                "instant": instant,
                "state": state_at,
                "fund_location": self._fund_location(inst, as_of=instant) if payer_scope else "***",
                "available_balance": {currency: self.ledger.account_balance(self._journal(inst), "available", currency=currency, as_of=instant) for currency in inst["composition"]} if payer_scope else "***",
            }
        return view

    def history(self, context: AccessContext, business_key: str) -> list[dict]:
        context.require("history:payments")
        inst = self._get(business_key)
        return [self._redact(row, context) for row in self.repository.history(INSTRUCTION_TYPE, inst["entity_id"])]

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _get(self, business_key: str) -> dict:
        rows = self.repository.search(INSTRUCTION_TYPE, "business_key", business_key)
        if not rows:
            raise NotFoundError(f"付款指令 {business_key} 不存在")
        return rows[0]

    def _journal(self, inst: Mapping[str, object]) -> str:
        return self._journal_for(str(inst["payer_org"]))

    @staticmethod
    def _journal_for(payer_org: str) -> str:
        return f"funds:{payer_org}"

    def journal_of(self, inst: Mapping[str, object]) -> str:
        return self._journal(inst)

    @staticmethod
    def _require_side(context: AccessContext, side: str) -> None:
        if not context.has_scope(side):
            raise PermissionDenied(f"缺少范围: {side}")

    def _fund_location(self, inst: dict, *, as_of: str | None = None) -> dict:
        prefix = f"{inst['business_key']}:"
        location: dict[str, dict[str, int]] = {}
        for entry in self.ledger.entries(self._journal(inst)):
            if not entry["reference"].startswith(prefix):
                continue
            if as_of is not None and entry["occurred_at"] > as_of:
                continue
            slot = location.setdefault(entry["currency"], {})
            signed = entry["amount_minor"] if entry["direction"] == "debit" else -entry["amount_minor"]
            slot[entry["account"]] = slot.get(entry["account"], 0) + signed
        return location

    def _settlement_summary(self, inst: dict, payer_scope: bool) -> dict:
        summary: dict[str, dict] = {}
        for currency, slot in inst["composition"].items():
            settled = inst["settled"].get(currency)
            refunded = inst["refunded"].get(currency, 0)
            row = {
                "gross_minor": slot["gross_minor"],
                "net_minor": slot["net_minor"],
                "settled_net_minor": settled["net_minor"] if settled else 0,
                "refunded_minor": refunded,
                "outstanding_minor": (settled["net_minor"] - refunded) if settled else 0,
            }
            if payer_scope:
                row["fee_minor"] = slot["fee_minor"]
            summary[currency] = row
        return summary

    def _redact(self, record: Mapping[str, object], context: AccessContext) -> dict:
        view = dict(record)
        if not context.has_scope("side:payer"):
            view["water_level"] = "***"
            if isinstance(view.get("payer_profile"), Mapping):
                view["payer_profile"] = {**view["payer_profile"], "account": "***"}
            if isinstance(view.get("components"), list):
                view["components"] = [{**line, "fee_minor": "***"} if isinstance(line, Mapping) else line for line in view["components"]]
            if isinstance(view.get("composition"), Mapping):
                view["composition"] = {currency: {**slot, "fee_minor": "***"} if isinstance(slot, Mapping) else slot for currency, slot in view["composition"].items()}
        if not context.has_scope("side:payee") and isinstance(view.get("payee_profile"), Mapping):
            view["payee_profile"] = {**view["payee_profile"], "account": "***"}
        return view

    @staticmethod
    def _redact_item(item: Mapping[str, object], context: AccessContext) -> dict:
        view = dict(item)
        if not context.has_scope("side:payee"):
            for key in ("expected", "actual"):
                if isinstance(view.get(key), Mapping) and "payee_account" in view[key]:
                    view[key] = {**view[key], "payee_account": "***"}
        return view
