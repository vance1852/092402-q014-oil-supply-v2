"""转运排放核算：因子版本、签收核算、季度封存与调整单。

核算口径：
- 能耗因子按（线路、油品、生效时间）保存互不重叠的版本，区间的右端由后继版本界定；
- 因子版本一旦被核算条目引用即不可修改，未引用的版本可以撤销后重新登记；
- 计算只认最终签收数量，标准损耗与争议损耗分别呈现；
- 每条核算条目固化因子快照和精度规则，历史结果不随新因子回算；
- 季度未封存时负责人可以预览，封存后迟到签收或因子更正只能开调整单滚入下一期。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .models import PRODUCTS, decimal_value, identifier, required_text
from .planning import (
    canonical_json,
    decimal_text,
    quantize_emission,
    quantize_factor,
    quantize_volume,
)
from .service import append_audit_event, require_permission
from .storage import initialize, transaction


QUARTER_FORMAT = re.compile(r"^[0-9]{4}Q[1-4]$")
RETROFIT_STAGES = {"pre_retrofit", "post_retrofit"}
ADJUSTMENT_TYPES = {"late_receipt", "factor_correction"}
ZERO = Decimal("0")

# 参与内容摘要的报表字段，顺序固定，保证导出字节稳定。
CONTENT_KEYS = ("quarter", "precision", "entries", "adjustments", "in_transit", "totals")


def precision_rules() -> dict[str, str]:
    """核算全程固化的精度规则快照。"""
    return {
        "quantity_quantum": "0.001",
        "emission_quantum": "0.001",
        "factor_quantum": "0.000001",
        "rounding": "ROUND_HALF_UP",
    }


def quarter_of(moment: datetime) -> str:
    return f"{moment.year}Q{(moment.month - 1) // 3 + 1}"


def validate_quarter(value: object, field: str = "quarter") -> str:
    text = required_text(value, field, 6)
    if not QUARTER_FORMAT.fullmatch(text):
        raise ValidationFailed(f"{field} 必须是形如 2026Q1 的季度")
    return text


def quarter_bounds(quarter: str) -> tuple[datetime, datetime]:
    year = int(quarter[:4])
    index = int(quarter[-1])
    start_month = (index - 1) * 3 + 1
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    if index == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, start_month + 3, 1, tzinfo=timezone.utc)
    return start, end


def next_quarter(quarter: str) -> str:
    year = int(quarter[:4])
    index = int(quarter[-1])
    if index == 4:
        return f"{year + 1}Q1"
    return f"{year}Q{index + 1}"


def _content_digest(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


class EmissionsService:
    """按签收数量核算转运排放，季度封存后只通过调整单滚动。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        return require_permission(self.connection, user_id, permission)

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        append_audit_event(
            self.connection,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor_id=actor_id,
            payload=payload,
            created_at=self._now(),
        )

    # ------------------------------------------------------------------
    # 能耗因子版本
    # ------------------------------------------------------------------

    def register_factor(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "emissions.factor.write")
        route_id = identifier(raw.get("route_id"), "route_id")
        product = required_text(raw.get("product"), "product", 32)
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的油品")
        stage = required_text(raw.get("retrofit_stage"), "retrofit_stage", 24)
        if stage not in RETROFIT_STAGES:
            raise ValidationFailed("retrofit_stage 必须是 pre_retrofit 或 post_retrofit")
        route = self.connection.execute(
            "SELECT * FROM routes WHERE route_id=?", (route_id,)
        ).fetchone()
        if route is None:
            raise NotFound("线路不存在")
        if route["product"] != product:
            raise ValidationFailed("油品与线路油品不一致")
        value = quantize_factor(
            decimal_value(
                raw.get("factor_value"),
                "factor_value",
                minimum=Decimal("0.000001"),
                maximum=Decimal("1000"),
            )
        )
        try:
            effective = utc_text(
                parse_utc(required_text(raw.get("effective_from"), "effective_from", 40), "effective_from")
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO emission_factors(route_id,product,retrofit_stage,factor_value,effective_from,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (route_id, product, stage, decimal_text(value), effective, actor_id, self._now()),
                )
                factor_id = int(cursor.lastrowid)
                self._audit(
                    "emission_factor",
                    str(factor_id),
                    "emission_factor.registered",
                    actor_id,
                    {"route_id": route_id, "product": product, "effective_from": effective},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一生效时间的因子版本已存在") from exc
        row = self.connection.execute(
            "SELECT * FROM emission_factors WHERE factor_id=?", (factor_id,)
        ).fetchone()
        return self._factor_view(row, None)

    def retire_factor(self, actor_id: str, factor_id: int) -> dict[str, Any]:
        self._require(actor_id, "emissions.factor.write")
        row = self.connection.execute(
            "SELECT * FROM emission_factors WHERE factor_id=?", (factor_id,)
        ).fetchone()
        if row is None:
            raise NotFound("因子版本不存在")
        if row["referenced"]:
            raise Conflict("因子已被核算引用，不可修改")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "DELETE FROM emission_factors WHERE factor_id=? AND referenced=0", (factor_id,)
            )
            if cursor.rowcount != 1:
                raise Conflict("因子已被核算引用，不可修改")
            self._audit(
                "emission_factor",
                str(factor_id),
                "emission_factor.retired",
                actor_id,
                {"route_id": row["route_id"], "product": row["product"]},
            )
        return {"factor_id": factor_id, "retired": True}

    def list_factors(self, actor_id: str, route_id: str, product: str) -> dict[str, Any]:
        self._require(actor_id, "emissions.read")
        route_id = identifier(route_id, "route_id")
        product = required_text(product, "product", 32)
        rows = self.connection.execute(
            "SELECT * FROM emission_factors WHERE route_id=? AND product=? "
            "ORDER BY effective_from, factor_id",
            (route_id, product),
        ).fetchall()
        versions = []
        for index, row in enumerate(rows):
            effective_to = rows[index + 1]["effective_from"] if index + 1 < len(rows) else None
            versions.append(self._factor_view(row, effective_to))
        return {"route_id": route_id, "product": product, "versions": versions}

    @staticmethod
    def _factor_view(row: sqlite3.Row, effective_to: str | None) -> dict[str, Any]:
        return {
            "factor_id": row["factor_id"],
            "retrofit_stage": row["retrofit_stage"],
            "factor_value": row["factor_value"],
            "effective_from": row["effective_from"],
            "effective_to": effective_to,
            "referenced": bool(row["referenced"]),
        }

    def _factor_at(self, route_id: str, product: str, moment_text: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM emission_factors WHERE route_id=? AND product=? AND effective_from<=? "
            "ORDER BY effective_from DESC, factor_id DESC LIMIT 1",
            (route_id, product, moment_text),
        ).fetchone()
        if row is None:
            raise InvalidState("签收时间没有生效的能耗因子")
        return row

    # ------------------------------------------------------------------
    # 签收与核算条目
    # ------------------------------------------------------------------

    def _transfer_context(self, transfer_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT t.*,n.route_id,r.product FROM transfers t "
            "JOIN nominations n ON n.nomination_id=t.nomination_id "
            "JOIN routes r ON r.route_id=n.route_id WHERE t.transfer_id=?",
            (transfer_id,),
        ).fetchone()
        if row is None:
            raise NotFound("转运不存在")
        return row

    def _parse_signed(self, raw: Mapping[str, Any], transfer: sqlite3.Row) -> tuple[datetime, Decimal, str]:
        try:
            moment = parse_utc(required_text(raw.get("signed_at"), "signed_at", 40), "signed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if moment > self.clock.now():
            raise ValidationFailed("signed_at 不能晚于当前时间")
        departed = parse_utc(transfer["departed_at"], "departed_at")
        if moment < departed:
            raise ValidationFailed("signed_at 不能早于发运时间")
        signed = quantize_volume(decimal_value(raw.get("signed_barrels"), "signed_barrels", minimum=ZERO))
        if signed > Decimal(transfer["loaded_barrels"]):
            raise ValidationFailed("签收数量不能超过装船数量")
        return moment, signed, utc_text(moment)

    @staticmethod
    def _compute(transfer: sqlite3.Row, signed: Decimal, factor: sqlite3.Row) -> dict[str, Decimal]:
        loaded = Decimal(transfer["loaded_barrels"])
        expected = Decimal(transfer["expected_delivered_barrels"])
        return {
            "standard_loss": quantize_volume(loaded - expected),
            "disputed_loss": quantize_volume(expected - signed),
            "emissions": quantize_emission(signed * Decimal(factor["factor_value"])),
        }

    def _insert_receipt(
        self,
        actor_id: str,
        transfer_id: str,
        signed: Decimal,
        signed_text: str,
        quarter: str,
        computed: Mapping[str, Decimal],
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO transfer_receipts(transfer_id,signed_barrels,standard_loss_barrels,"
            "disputed_loss_barrels,signed_at,quarter,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                transfer_id,
                decimal_text(signed),
                decimal_text(computed["standard_loss"]),
                decimal_text(computed["disputed_loss"]),
                signed_text,
                quarter,
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def _insert_entry(
        self,
        actor_id: str,
        transfer: sqlite3.Row,
        receipt_id: int,
        signed: Decimal,
        quarter: str,
        factor: sqlite3.Row,
        computed: Mapping[str, Decimal],
        adjustment_id: int | None,
    ) -> int:
        rules = precision_rules()
        cursor = self.connection.execute(
            "INSERT INTO emission_entries(receipt_id,transfer_id,route_id,product,quarter,signed_barrels,"
            "factor_id,factor_value,retrofit_stage,factor_effective_from,emissions_kg,quantity_quantum,"
            "emission_quantum,factor_quantum,rounding,adjustment_id,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                transfer["transfer_id"],
                transfer["route_id"],
                transfer["product"],
                quarter,
                decimal_text(signed),
                factor["factor_id"],
                factor["factor_value"],
                factor["retrofit_stage"],
                factor["effective_from"],
                decimal_text(computed["emissions"]),
                rules["quantity_quantum"],
                rules["emission_quantum"],
                rules["factor_quantum"],
                rules["rounding"],
                adjustment_id,
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def _close_transfer(self, transfer_id: str, signed_text: str, disputed_loss: Decimal) -> None:
        state = "disputed" if disputed_loss > ZERO else "delivered"
        self.connection.execute(
            "UPDATE transfers SET state=?,arrived_at=?,revision=revision+1 "
            "WHERE transfer_id=? AND state='in_transit'",
            (state, signed_text, transfer_id),
        )

    def record_receipt(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "emissions.receipt.write")
        transfer_id = identifier(raw.get("transfer_id"), "transfer_id")
        transfer = self._transfer_context(transfer_id)
        moment, signed, signed_text = self._parse_signed(raw, transfer)
        existing = self.connection.execute(
            "SELECT * FROM transfer_receipts WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if existing is not None:
            if existing["signed_barrels"] == decimal_text(signed) and existing["signed_at"] == signed_text:
                return self._entry_view_by_transfer(transfer_id)
            raise Conflict("转运已签收，签收信息不一致")
        quarter = quarter_of(moment)
        if self._is_sealed(quarter):
            raise InvalidState("季度已封存，迟到签收只能通过调整单登记")
        factor = self._factor_at(transfer["route_id"], transfer["product"], signed_text)
        computed = self._compute(transfer, signed, factor)
        with transaction(self.connection, immediate=True):
            receipt_id = self._insert_receipt(actor_id, transfer_id, signed, signed_text, quarter, computed)
            entry_id = self._insert_entry(
                actor_id, transfer, receipt_id, signed, quarter, factor, computed, None
            )
            self.connection.execute(
                "UPDATE emission_factors SET referenced=1 WHERE factor_id=?", (factor["factor_id"],)
            )
            self._close_transfer(transfer_id, signed_text, computed["disputed_loss"])
            self._audit(
                "transfer",
                transfer_id,
                "transfer.receipt.recorded",
                actor_id,
                {
                    "receipt_id": receipt_id,
                    "entry_id": entry_id,
                    "quarter": quarter,
                    "emissions_kg": decimal_text(computed["emissions"]),
                },
            )
        return self._entry_view(entry_id)

    def _entry_row(self, clause: str, parameter: object) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT e.*,r.signed_at,r.standard_loss_barrels,r.disputed_loss_barrels "
            "FROM emission_entries e JOIN transfer_receipts r ON r.receipt_id=e.receipt_id "
            f"WHERE {clause}",
            (parameter,),
        ).fetchone()

    def _entry_view(self, entry_id: int) -> dict[str, Any]:
        row = self._entry_row("e.entry_id=?", entry_id)
        return self._entry_view_from_row(row)

    def _entry_view_by_transfer(self, transfer_id: str) -> dict[str, Any]:
        row = self._entry_row("e.transfer_id=?", transfer_id)
        return self._entry_view_from_row(row)

    @staticmethod
    def _entry_view_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "entry_id": row["entry_id"],
            "transfer_id": row["transfer_id"],
            "route_id": row["route_id"],
            "product": row["product"],
            "quarter": row["quarter"],
            "signed_at": row["signed_at"],
            "signed_barrels": row["signed_barrels"],
            "standard_loss_barrels": row["standard_loss_barrels"],
            "disputed_loss_barrels": row["disputed_loss_barrels"],
            "factor": {
                "factor_id": row["factor_id"],
                "retrofit_stage": row["retrofit_stage"],
                "factor_value": row["factor_value"],
                "effective_from": row["factor_effective_from"],
            },
            "emissions_kg": row["emissions_kg"],
            "precision": {
                "quantity_quantum": row["quantity_quantum"],
                "emission_quantum": row["emission_quantum"],
                "factor_quantum": row["factor_quantum"],
                "rounding": row["rounding"],
            },
            "adjustment_id": row["adjustment_id"],
        }

    def entry_for_transfer(self, actor_id: str, transfer_id: str) -> dict[str, Any]:
        self._require(actor_id, "emissions.read")
        row = self._entry_row("e.transfer_id=?", transfer_id)
        if row is None:
            raise NotFound("转运尚未签收核算")
        return self._entry_view_from_row(row)

    # ------------------------------------------------------------------
    # 季度报表：预览、封存、导出
    # ------------------------------------------------------------------

    def _is_sealed(self, quarter: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM emission_statements WHERE quarter=?", (quarter,)
            ).fetchone()
            is not None
        )

    def _statement_content(self, quarter: str) -> dict[str, Any]:
        _, end = quarter_bounds(quarter)
        entry_rows = self.connection.execute(
            "SELECT e.*,r.signed_at,r.standard_loss_barrels,r.disputed_loss_barrels "
            "FROM emission_entries e JOIN transfer_receipts r ON r.receipt_id=e.receipt_id "
            "WHERE e.quarter=? AND e.adjustment_id IS NULL ORDER BY r.signed_at,e.transfer_id",
            (quarter,),
        ).fetchall()
        entries = [
            {
                "entry_id": row["entry_id"],
                "transfer_id": row["transfer_id"],
                "route_id": row["route_id"],
                "product": row["product"],
                "signed_at": row["signed_at"],
                "signed_barrels": row["signed_barrels"],
                "standard_loss_barrels": row["standard_loss_barrels"],
                "disputed_loss_barrels": row["disputed_loss_barrels"],
                "factor": {
                    "factor_id": row["factor_id"],
                    "retrofit_stage": row["retrofit_stage"],
                    "factor_value": row["factor_value"],
                    "effective_from": row["factor_effective_from"],
                },
                "emissions_kg": row["emissions_kg"],
            }
            for row in entry_rows
        ]
        adjustment_rows = self.connection.execute(
            "SELECT * FROM emission_adjustments WHERE target_quarter=? ORDER BY adjustment_id",
            (quarter,),
        ).fetchall()
        adjustments = [
            {
                "adjustment_id": row["adjustment_id"],
                "adjustment_type": row["adjustment_type"],
                "source_quarter": row["source_quarter"],
                "transfer_id": row["transfer_id"],
                "route_id": row["route_id"],
                "product": row["product"],
                "signed_barrels": row["signed_barrels"],
                "standard_loss_barrels": row["standard_loss_barrels"],
                "disputed_loss_barrels": row["disputed_loss_barrels"],
                "delta_emissions_kg": row["delta_emissions_kg"],
                "reason": row["reason"],
            }
            for row in adjustment_rows
        ]
        transit_rows = self.connection.execute(
            "SELECT t.transfer_id,t.departed_at,t.loaded_barrels,n.route_id FROM transfers t "
            "JOIN nominations n ON n.nomination_id=t.nomination_id "
            "WHERE t.state='in_transit' AND t.departed_at<? ORDER BY t.departed_at,t.transfer_id",
            (utc_text(end),),
        ).fetchall()
        in_transit = [
            {
                "transfer_id": row["transfer_id"],
                "route_id": row["route_id"],
                "departed_at": row["departed_at"],
                "loaded_barrels": row["loaded_barrels"],
            }
            for row in transit_rows
        ]
        content: dict[str, Any] = {
            "quarter": quarter,
            "precision": precision_rules(),
            "entries": entries,
            "adjustments": adjustments,
            "in_transit": in_transit,
            "totals": self._totals(entries, adjustments),
        }
        content["content_sha256"] = _content_digest(content)
        return content

    @staticmethod
    def _totals(
        entries: list[Mapping[str, Any]], adjustments: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        def sum_of(rows: list[Mapping[str, Any]], key: str) -> Decimal:
            return sum((Decimal(str(row[key])) for row in rows), ZERO)

        base = {
            "entries": len(entries),
            "signed_barrels": decimal_text(quantize_volume(sum_of(entries, "signed_barrels"))),
            "standard_loss_barrels": decimal_text(
                quantize_volume(sum_of(entries, "standard_loss_barrels"))
            ),
            "disputed_loss_barrels": decimal_text(
                quantize_volume(sum_of(entries, "disputed_loss_barrels"))
            ),
            "emissions_kg": decimal_text(quantize_emission(sum_of(entries, "emissions_kg"))),
        }
        adjusted = {
            "orders": len(adjustments),
            "signed_barrels": decimal_text(quantize_volume(sum_of(adjustments, "signed_barrels"))),
            "standard_loss_barrels": decimal_text(
                quantize_volume(sum_of(adjustments, "standard_loss_barrels"))
            ),
            "disputed_loss_barrels": decimal_text(
                quantize_volume(sum_of(adjustments, "disputed_loss_barrels"))
            ),
            "emissions_kg": decimal_text(quantize_emission(sum_of(adjustments, "delta_emissions_kg"))),
        }
        grand = {
            key: decimal_text(
                quantize_emission(Decimal(base[key]) + Decimal(adjusted[key]))
                if key == "emissions_kg"
                else quantize_volume(Decimal(base[key]) + Decimal(adjusted[key]))
            )
            for key in ("signed_barrels", "standard_loss_barrels", "disputed_loss_barrels", "emissions_kg")
        }
        return {"base": base, "adjustments": adjusted, "grand": grand}

    def statement(self, actor_id: str, quarter: str) -> dict[str, Any]:
        quarter = validate_quarter(quarter)
        sealed = self.connection.execute(
            "SELECT * FROM emission_statements WHERE quarter=?", (quarter,)
        ).fetchone()
        if sealed is not None:
            self._require(actor_id, "emissions.read")
            payload = json.loads(sealed["result_json"])
            content = {key: payload[key] for key in CONTENT_KEYS}
            digest = _content_digest(content)
            return {
                **payload,
                "integrity_valid": digest == sealed["content_sha256"]
                and digest == payload.get("content_sha256"),
            }
        self._require(actor_id, "emissions.statement.preview")
        return {**self._statement_content(quarter), "state": "open"}

    def seal_statement(self, actor_id: str, quarter: str) -> dict[str, Any]:
        self._require(actor_id, "emissions.statement.seal")
        quarter = validate_quarter(quarter)
        if self._is_sealed(quarter):
            raise InvalidState("季度已封存")
        sealed_at = self._now()
        payload = {
            **self._statement_content(quarter),
            "state": "sealed",
            "sealed_by": actor_id,
            "sealed_at": sealed_at,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO emission_statements(quarter,sealed_by,sealed_at,result_json,content_sha256) "
                    "VALUES(?,?,?,?,?)",
                    (
                        quarter,
                        actor_id,
                        sealed_at,
                        canonical_json(payload),
                        payload["content_sha256"],
                    ),
                )
                self._audit(
                    "emission_statement",
                    quarter,
                    "emission_statement.sealed",
                    actor_id,
                    {"content_sha256": payload["content_sha256"]},
                )
        except sqlite3.IntegrityError as exc:
            raise InvalidState("季度已封存") from exc
        return {**payload, "integrity_valid": True}

    def export_quarter(self, actor_id: str, quarter: str) -> dict[str, Any]:
        """命令行导出的稳定视图：不含任何时钟字段，顺序与摘要固定。"""
        view = self.statement(actor_id, quarter)
        export = {key: view[key] for key in (*CONTENT_KEYS, "content_sha256", "state")}
        for key in ("sealed_by", "sealed_at", "integrity_valid"):
            if key in view:
                export[key] = view[key]
        return export

    # ------------------------------------------------------------------
    # 调整单：封存后唯一的滚动通道
    # ------------------------------------------------------------------

    def _first_open_quarter_after(self, quarter: str) -> str:
        candidate = next_quarter(quarter)
        for _ in range(40):
            if not self._is_sealed(candidate):
                return candidate
            candidate = next_quarter(candidate)
        raise InvalidState("找不到未封存的后续季度")

    def open_adjustment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "emissions.adjustment.write")
        adjustment_type = required_text(raw.get("adjustment_type"), "adjustment_type", 32)
        if adjustment_type not in ADJUSTMENT_TYPES:
            raise ValidationFailed("adjustment_type 必须是 late_receipt 或 factor_correction")
        reason = required_text(raw.get("reason"), "reason")
        if adjustment_type == "late_receipt":
            return self._open_late_receipt(actor_id, raw, reason)
        return self._open_factor_correction(actor_id, raw, reason)

    def _insert_adjustment(
        self,
        actor_id: str,
        *,
        adjustment_type: str,
        source: str,
        target: str,
        transfer_id: str | None,
        receipt_id: int | None,
        route_id: str | None,
        product: str | None,
        signed: Decimal,
        standard_loss: Decimal,
        disputed_loss: Decimal,
        delta: Decimal,
        reason: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO emission_adjustments(adjustment_type,source_quarter,target_quarter,transfer_id,"
            "receipt_id,route_id,product,signed_barrels,standard_loss_barrels,disputed_loss_barrels,"
            "delta_emissions_kg,reason,precision_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                adjustment_type,
                source,
                target,
                transfer_id,
                receipt_id,
                route_id,
                product,
                decimal_text(quantize_volume(signed)),
                decimal_text(quantize_volume(standard_loss)),
                decimal_text(quantize_volume(disputed_loss)),
                decimal_text(quantize_emission(delta)),
                reason,
                canonical_json(precision_rules()),
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def _adjustment_view(self, adjustment_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM emission_adjustments WHERE adjustment_id=?", (adjustment_id,)
        ).fetchone()
        return {
            "adjustment_id": row["adjustment_id"],
            "adjustment_type": row["adjustment_type"],
            "source_quarter": row["source_quarter"],
            "target_quarter": row["target_quarter"],
            "transfer_id": row["transfer_id"],
            "route_id": row["route_id"],
            "product": row["product"],
            "signed_barrels": row["signed_barrels"],
            "standard_loss_barrels": row["standard_loss_barrels"],
            "disputed_loss_barrels": row["disputed_loss_barrels"],
            "delta_emissions_kg": row["delta_emissions_kg"],
            "reason": row["reason"],
        }

    def _open_late_receipt(
        self, actor_id: str, raw: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        transfer_id = identifier(raw.get("transfer_id"), "transfer_id")
        transfer = self._transfer_context(transfer_id)
        moment, signed, signed_text = self._parse_signed(raw, transfer)
        if (
            self.connection.execute(
                "SELECT 1 FROM transfer_receipts WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            is not None
        ):
            raise Conflict("转运已签收")
        source = quarter_of(moment)
        if not self._is_sealed(source):
            raise InvalidState("季度未封存，可直接登记签收")
        target = self._first_open_quarter_after(source)
        factor = self._factor_at(transfer["route_id"], transfer["product"], signed_text)
        computed = self._compute(transfer, signed, factor)
        with transaction(self.connection, immediate=True):
            receipt_id = self._insert_receipt(actor_id, transfer_id, signed, signed_text, source, computed)
            adjustment_id = self._insert_adjustment(
                actor_id,
                adjustment_type="late_receipt",
                source=source,
                target=target,
                transfer_id=transfer_id,
                receipt_id=receipt_id,
                route_id=transfer["route_id"],
                product=transfer["product"],
                signed=signed,
                standard_loss=computed["standard_loss"],
                disputed_loss=computed["disputed_loss"],
                delta=computed["emissions"],
                reason=reason,
            )
            entry_id = self._insert_entry(
                actor_id, transfer, receipt_id, signed, source, factor, computed, adjustment_id
            )
            self.connection.execute(
                "UPDATE emission_factors SET referenced=1 WHERE factor_id=?", (factor["factor_id"],)
            )
            self._close_transfer(transfer_id, signed_text, computed["disputed_loss"])
            self._audit(
                "emission_adjustment",
                str(adjustment_id),
                "emission_adjustment.opened",
                actor_id,
                {"adjustment_type": "late_receipt", "source_quarter": source, "target_quarter": target},
            )
        return {
            "adjustment": self._adjustment_view(adjustment_id),
            "entry": self._entry_view(entry_id),
        }

    def _open_factor_correction(
        self, actor_id: str, raw: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        source = validate_quarter(raw.get("source_quarter"), "source_quarter")
        route_id = identifier(raw.get("route_id"), "route_id")
        product = required_text(raw.get("product"), "product", 32)
        if not self._is_sealed(source):
            raise InvalidState("季度未封存，无需调整单")
        rows = self.connection.execute(
            "SELECT e.*,r.signed_at FROM emission_entries e "
            "JOIN transfer_receipts r ON r.receipt_id=e.receipt_id "
            "WHERE e.quarter=? AND e.route_id=? AND e.product=? ORDER BY e.entry_id",
            (source, route_id, product),
        ).fetchall()
        if not rows:
            raise InvalidState("封存季度内没有该线路油品的核算条目")
        total_delta = ZERO
        for row in rows:
            factor = self._factor_at(route_id, product, row["signed_at"])
            recomputed = quantize_emission(Decimal(row["signed_barrels"]) * Decimal(factor["factor_value"]))
            total_delta += recomputed - Decimal(row["emissions_kg"])
        booked = self.connection.execute(
            "SELECT delta_emissions_kg FROM emission_adjustments "
            "WHERE adjustment_type='factor_correction' AND source_quarter=? AND route_id=? AND product=?",
            (source, route_id, product),
        ).fetchall()
        already = sum((Decimal(row["delta_emissions_kg"]) for row in booked), ZERO)
        delta = quantize_emission(total_delta - already)
        if delta == ZERO:
            raise InvalidState("因子更正没有产生新的差异")
        target = self._first_open_quarter_after(source)
        with transaction(self.connection, immediate=True):
            adjustment_id = self._insert_adjustment(
                actor_id,
                adjustment_type="factor_correction",
                source=source,
                target=target,
                transfer_id=None,
                receipt_id=None,
                route_id=route_id,
                product=product,
                signed=ZERO,
                standard_loss=ZERO,
                disputed_loss=ZERO,
                delta=delta,
                reason=reason,
            )
            self._audit(
                "emission_adjustment",
                str(adjustment_id),
                "emission_adjustment.opened",
                actor_id,
                {
                    "adjustment_type": "factor_correction",
                    "source_quarter": source,
                    "target_quarter": target,
                    "delta_emissions_kg": decimal_text(delta),
                },
            )
        return {"adjustment": self._adjustment_view(adjustment_id)}
