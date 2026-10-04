"""不可变资金分录与冲正。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from .database import Database
from .errors import ConflictError, InvariantViolation, NotFoundError, ValidationError
from .identifiers import new_id, require_safe
from .timeutil import Clock, canonical_instant


def to_minor(value: str | int | Decimal, exponent: int = 2) -> int:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError("金额格式错误") from exc
    if not number.is_finite():
        raise ValidationError("金额必须是有限数")
    quantum = Decimal(1).scaleb(-exponent)
    return int(number.quantize(quantum, rounding=ROUND_HALF_EVEN).scaleb(exponent))


def from_minor(value: int, exponent: int = 2) -> str:
    return format(Decimal(int(value)).scaleb(-exponent), "f")


@dataclass(frozen=True)
class Ledger:
    database: Database
    clock: Clock

    def post(self, *, journal_key: str, account: str, currency: str, amount: str, direction: str, reference: str, actor: str) -> dict:
        require_safe(journal_key, "账簿标识"); require_safe(currency, "币种")
        if direction not in {"debit", "credit"}:
            raise ValidationError("方向必须是 debit 或 credit")
        minor = to_minor(amount)
        if minor <= 0:
            raise ValidationError("金额必须大于零")
        entry_id = new_id("entry")
        with self.database.transaction() as connection:
            duplicate = connection.execute("SELECT entry_id FROM journal_entries WHERE journal_key=? AND reference=? AND direction=?", (journal_key, reference, direction)).fetchone()
            if duplicate:
                raise ConflictError("相同参考号和方向已经入账")
            connection.execute("INSERT INTO journal_entries(entry_id,journal_key,account,currency,amount_minor,direction,reference,occurred_at,posted_by) VALUES(?,?,?,?,?,?,?,?,?)", (entry_id, journal_key, account, currency, minor, direction, reference, self.clock.now(), actor))
        return {"entry_id": entry_id, "amount_minor": minor, "direction": direction}

    def reverse(self, entry_id: str, *, reference: str, actor: str) -> dict:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM journal_entries WHERE entry_id=?", (entry_id,)).fetchone()
            if not row:
                raise NotFoundError("原分录不存在")
            existing = connection.execute("SELECT entry_id FROM journal_entries WHERE reversed_entry_id=?", (entry_id,)).fetchone()
            if existing:
                return {"entry_id": existing["entry_id"], "replayed": True}
            reversal = new_id("entry"); direction = "credit" if row["direction"] == "debit" else "debit"
            connection.execute("INSERT INTO journal_entries(entry_id,journal_key,account,currency,amount_minor,direction,reference,reversed_entry_id,occurred_at,posted_by) VALUES(?,?,?,?,?,?,?,?,?,?)", (reversal, row["journal_key"], row["account"], row["currency"], row["amount_minor"], direction, reference, entry_id, self.clock.now(), actor))
            return {"entry_id": reversal, "replayed": False}

    def post_if_absent(self, *, journal_key: str, account: str, currency: str, amount_minor: int, direction: str, reference: str, actor: str) -> dict:
        """按参考号幂等入账：已存在且内容一致则返回原分录，内容矛盾视为不变量破坏。"""
        require_safe(journal_key, "账簿标识"); require_safe(currency, "币种")
        if direction not in {"debit", "credit"}:
            raise ValidationError("方向必须是 debit 或 credit")
        if not isinstance(amount_minor, int) or amount_minor <= 0:
            raise ValidationError("金额必须为正整数最小单位")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM journal_entries WHERE journal_key=? AND reference=? AND direction=?", (journal_key, reference, direction)).fetchone()
            if row:
                if row["account"] != account or row["currency"] != currency or int(row["amount_minor"]) != amount_minor:
                    raise InvariantViolation(f"参考号 {reference} 已对应不同内容的分录")
                return {"entry_id": row["entry_id"], "amount_minor": int(row["amount_minor"]), "direction": direction, "replayed": True}
            entry_id = new_id("entry")
            connection.execute("INSERT INTO journal_entries(entry_id,journal_key,account,currency,amount_minor,direction,reference,occurred_at,posted_by) VALUES(?,?,?,?,?,?,?,?,?)", (entry_id, journal_key, account, currency, amount_minor, direction, reference, self.clock.now(), actor))
            return {"entry_id": entry_id, "amount_minor": amount_minor, "direction": direction, "replayed": False}

    def entries(self, journal_key: str) -> list[dict]:
        require_safe(journal_key, "账簿标识")
        with self.database.connect() as connection:
            rows = connection.execute("SELECT rowid AS seq,* FROM journal_entries WHERE journal_key=? ORDER BY rowid", (journal_key,)).fetchall()
        return [{"entry_id": row["entry_id"], "account": row["account"], "currency": row["currency"], "amount_minor": int(row["amount_minor"]), "direction": row["direction"], "reference": row["reference"], "reversed_entry_id": row["reversed_entry_id"], "occurred_at": row["occurred_at"], "posted_by": row["posted_by"]} for row in rows]

    def balance(self, journal_key: str, *, currency: str, as_of: str | None = None) -> int:
        return self._sum(journal_key, None, currency, as_of)

    def account_balance(self, journal_key: str, account: str, *, currency: str, as_of: str | None = None) -> int:
        return self._sum(journal_key, account, currency, as_of)

    def _sum(self, journal_key: str, account: str | None, currency: str, as_of: str | None) -> int:
        sql = "SELECT COALESCE(SUM(CASE direction WHEN 'debit' THEN amount_minor ELSE -amount_minor END),0) AS value FROM journal_entries WHERE journal_key=? AND currency=?"
        params: list[object] = [journal_key, currency]
        if account is not None:
            sql += " AND account=?"; params.append(account)
        if as_of is not None:
            sql += " AND occurred_at<=?"; params.append(canonical_instant(as_of))
        with self.database.connect() as connection:
            row = connection.execute(sql, params).fetchone()
            return int(row["value"])
