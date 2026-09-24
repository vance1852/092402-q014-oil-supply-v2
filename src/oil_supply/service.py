"""报价、库存、线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .emissions import (
    DEFAULT_PRECISION,
    build_statement,
    export_document,
    next_quarter,
    parse_quarter,
    quarter_bounds,
    quarter_of,
    ranges_overlap,
    resolve_factor,
    transfer_emission,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    EmissionFactorInput,
    IndexQuote,
    Facility,
    InventoryLot,
    NominationRequest,
    Route,
    SupplyScenario,
    decimal_value,
    identifier,
    required_text,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "transfer.sign", "inventory.write"},
    "risk": {"outage.write", "scenario.approve", "report.read"},
    "auditor": {"report.read", "audit.read", "emission.read"},
    "emissions_officer": {"factor.write", "emission.read", "quarter.preview", "quarter.seal", "adjustment.write"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM price_index_quotes WHERE price_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.price_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO price_index_quotes(price_index,trade_date,close_usd,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.price_index,
                        quote.trade_date,
                        decimal_text(quote.close_usd),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"price_index": quote.price_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("报价版本冲突") from exc
        return {"quote_id": quote_id, "price_index": quote.price_index, "trade_date": quote.trade_date}

    def price_summary(self, price_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_usd FROM price_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM price_index_quotes "
            "WHERE price_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (price_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_usd"])) for row in rows]
        if not points:
            raise NotFound("没有基准报价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "price_index": price_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_usd": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_barrels,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_barrels),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("线路不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_barrels,available_barrels,"
                    "unit_cost_usd,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.quantity_barrels),
                        decimal_text(lot.unit_cost_usd),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("库存批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("库存批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_barrels,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_barrels),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_barrels"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_barrels"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_barrels=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_barrels"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可发运版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("库存批次不存在")
        allocated = Decimal(nomination["allocated_barrels"])
        available = Decimal(lot["available_barrels"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("库存批次与线路起点或油品不匹配")
        if available < allocated:
            raise Conflict("库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_barrels=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_barrels,"
                "expected_delivered_barrels,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_barrels": decimal_text(allocated),
            "expected_delivered_barrels": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_usd FROM price_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用报价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_barrels AS REAL)) available_barrels "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_usd"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_usd"]),
            price_index_drop_percent=scenario.price_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    @staticmethod
    def _factor_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "factor_id": row["factor_id"],
            "route_id": row["route_id"],
            "product": row["product"],
            "kgco2e_per_barrel": row["kgco2e_per_barrel"],
            "equipment_generation": row["equipment_generation"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
        }

    def _factor_versions(self, route_id: str, product: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM emission_factor_versions WHERE route_id=? AND product=? "
            "ORDER BY effective_from,factor_id",
            (route_id, product),
        ).fetchall()

    def _factor_for(self, route_id: str, product: str, moment) -> sqlite3.Row:
        version = resolve_factor(self._factor_versions(route_id, product), moment)
        if version is None:
            raise InvalidState(f"线路 {route_id} 的 {product} 在 {utc_text(moment)} 没有适用的能耗因子版本")
        return version

    def _ensure_factor_span_free(self, route_id, product, start, end, exclude) -> None:
        rows = self.connection.execute(
            "SELECT factor_id,effective_from,effective_to FROM emission_factor_versions "
            "WHERE route_id=? AND product=?",
            (route_id, product),
        ).fetchall()
        for row in rows:
            if row["factor_id"] == exclude:
                continue
            other_start = parse_utc(row["effective_from"], "effective_from")
            other_end = None if row["effective_to"] is None else parse_utc(row["effective_to"], "effective_to")
            if ranges_overlap(start, end, other_start, other_end):
                raise Conflict(f"因子生效区间与版本 {row['factor_id']} 重叠")

    def _valid_quarter(self, quarter_id: str) -> str:
        try:
            parse_quarter(quarter_id)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return quarter_id

    def _quarter_state(self, quarter_id: str) -> str:
        row = self.connection.execute(
            "SELECT state FROM emission_quarters WHERE quarter_id=?", (quarter_id,)
        ).fetchone()
        return "open" if row is None else row["state"]

    def _ensure_quarter(self, quarter_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO emission_quarters(quarter_id,created_at) VALUES(?,?)",
            (quarter_id, self._now()),
        )

    def _earliest_open_quarter_after(self, quarter_id: str) -> str:
        candidate = next_quarter(quarter_id)
        while self._quarter_state(candidate) == "sealed":
            candidate = next_quarter(candidate)
        return candidate

    def emission_factor(self, factor_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM emission_factor_versions WHERE factor_id=?", (factor_id,)
        ).fetchone()
        if row is None:
            raise NotFound("因子版本不存在")
        return dict(row)

    def register_emission_factor(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "factor.write")
        factor = EmissionFactorInput.from_dict(raw)
        route = self.route(factor.route_id)
        if route["product"] != factor.product:
            raise Conflict("因子油品与线路油品不一致")
        start = parse_utc(factor.effective_from, "effective_from")
        end = None if factor.effective_to is None else parse_utc(factor.effective_to, "effective_to")
        self._ensure_factor_span_free(factor.route_id, factor.product, start, end, exclude=None)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO emission_factor_versions(factor_id,route_id,product,kgco2e_per_barrel,"
                    "equipment_generation,effective_from,effective_to,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        factor.factor_id,
                        factor.route_id,
                        factor.product,
                        decimal_text(factor.kgco2e_per_barrel),
                        factor.equipment_generation,
                        factor.effective_from,
                        factor.effective_to,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "emission_factor",
                    factor.factor_id,
                    "factor.registered",
                    actor_id,
                    {"route_id": factor.route_id, "product": factor.product, "effective_from": factor.effective_from},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("因子编号已经存在") from exc
        return self.emission_factor(factor.factor_id)

    def correct_emission_factor(self, actor_id: str, factor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "factor.write")
        row = self.connection.execute(
            "SELECT * FROM emission_factor_versions WHERE factor_id=?", (factor_id,)
        ).fetchone()
        if row is None:
            raise NotFound("因子版本不存在")
        referenced = self.connection.execute(
            "SELECT 1 FROM emission_statement_factors f "
            "JOIN emission_quarters q ON q.quarter_id=f.quarter_id "
            "WHERE f.factor_id=? AND q.state='sealed' LIMIT 1",
            (factor_id,),
        ).fetchone()
        merged = {
            "factor_id": row["factor_id"],
            "route_id": row["route_id"],
            "product": row["product"],
            "kgco2e_per_barrel": raw.get("kgco2e_per_barrel", row["kgco2e_per_barrel"]),
            "equipment_generation": raw.get("equipment_generation", row["equipment_generation"]),
            "effective_from": raw.get("effective_from", row["effective_from"]),
            "effective_to": raw["effective_to"] if "effective_to" in raw else row["effective_to"],
        }
        factor = EmissionFactorInput.from_dict(merged)
        if referenced is not None:
            # 封存报表已固化因子快照，历史不可回算；因子值、设备代次和生效起点随之锁定，
            # 只允许调整生效止点，以便衔接后续版本且不影响已封存的核算结果。
            frozen = (
                decimal_text(factor.kgco2e_per_barrel) != row["kgco2e_per_barrel"]
                or factor.equipment_generation != row["equipment_generation"]
                or factor.effective_from != row["effective_from"]
            )
            if frozen:
                raise Conflict("因子已被封存核算引用，不能修改，请开调整单滚入下一期")
        start = parse_utc(factor.effective_from, "effective_from")
        end = None if factor.effective_to is None else parse_utc(factor.effective_to, "effective_to")
        self._ensure_factor_span_free(factor.route_id, factor.product, start, end, exclude=factor_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE emission_factor_versions SET kgco2e_per_barrel=?,equipment_generation=?,"
                "effective_from=?,effective_to=?,revision=revision+1 WHERE factor_id=? AND revision=?",
                (
                    decimal_text(factor.kgco2e_per_barrel),
                    factor.equipment_generation,
                    factor.effective_from,
                    factor.effective_to,
                    factor_id,
                    row["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise Conflict("因子版本已被并发修改")
            self._audit("emission_factor", factor_id, "factor.corrected", actor_id, {"revision": row["revision"] + 1})
        return self.emission_factor(factor_id)

    def list_emission_factors(self, actor_id: str, route_id: str, product: str) -> dict[str, Any]:
        self._require(actor_id, "emission.read")
        route_id = identifier(route_id, "route_id")
        product = required_text(product, "product", 32)
        return {
            "route_id": route_id,
            "product": product,
            "versions": [self._factor_dict(row) for row in self._factor_versions(route_id, product)],
        }

    def sign_transfer(self, actor_id: str, transfer_id: str, signed_barrels: object, signed_at: object) -> dict[str, Any]:
        self._require(actor_id, "transfer.sign")
        transfer = self.connection.execute(
            "SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if transfer is None:
            raise NotFound("转运不存在")
        if transfer["state"] != "in_transit":
            raise InvalidState("转运不在在途状态，不能签收")
        signed = decimal_value(signed_barrels, "signed_barrels", minimum=Decimal("0"))
        if signed > Decimal(transfer["loaded_barrels"]):
            raise ValidationFailed("签收数量不能超过装船数量")
        try:
            signed_dt = parse_utc(required_text(signed_at, "signed_at", 40), "signed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        departed_dt = parse_utc(transfer["departed_at"], "departed_at")
        if signed_dt < departed_dt:
            raise ValidationFailed("签收时间不能早于发运时间")
        if signed_dt > self.clock.now():
            raise ValidationFailed("签收时间不能晚于当前时间")
        quarter_id = quarter_of(signed_dt)
        signed_text = decimal_text(DEFAULT_PRECISION.volume(signed))
        adjustment = None
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO transfer_signoffs(transfer_id,signed_barrels,signed_at,signed_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (transfer_id, signed_text, utc_text(signed_dt), actor_id, self._now()),
            )
            cursor = self.connection.execute(
                "UPDATE transfers SET state='delivered',arrived_at=?,revision=revision+1 "
                "WHERE transfer_id=? AND state='in_transit'",
                (utc_text(signed_dt), transfer_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("转运不在在途状态，不能签收")
            self.connection.execute(
                "UPDATE nominations SET state='delivered',delivered_barrels=?,revision=revision+1 "
                "WHERE nomination_id=?",
                (signed_text, transfer["nomination_id"]),
            )
            self._audit(
                "transfer",
                transfer_id,
                "transfer.signed",
                actor_id,
                {"signed_barrels": signed_text, "signed_at": utc_text(signed_dt), "quarter_id": quarter_id},
            )
            if self._quarter_state(quarter_id) == "sealed":
                adjustment = self._create_late_signoff_adjustment(actor_id, transfer, signed, signed_dt, quarter_id)
        result = {
            "transfer_id": transfer_id,
            "state": "delivered",
            "signed_barrels": signed_text,
            "quarter_id": quarter_id,
        }
        if adjustment is not None:
            result["adjustment"] = adjustment
        return result

    def _create_late_signoff_adjustment(
        self,
        actor_id: str,
        transfer: sqlite3.Row,
        signed: Decimal,
        signed_dt,
        source_quarter_id: str,
    ) -> dict[str, Any]:
        route = self.connection.execute(
            "SELECT n.route_id,r.product FROM nominations n JOIN routes r ON r.route_id=n.route_id "
            "WHERE n.nomination_id=?",
            (transfer["nomination_id"],),
        ).fetchone()
        departed_dt = parse_utc(transfer["departed_at"], "departed_at")
        factor = self._factor_for(route["route_id"], route["product"], departed_dt)
        quantities = transfer_emission(
            loaded=Decimal(transfer["loaded_barrels"]),
            expected_delivered=Decimal(transfer["expected_delivered_barrels"]),
            signed=signed,
            kgco2e_per_barrel=Decimal(factor["kgco2e_per_barrel"]),
        )
        target = self._earliest_open_quarter_after(source_quarter_id)
        self._ensure_quarter(target)
        adjustment_id = f"adj-late-{transfer['transfer_id']}"
        detail = {
            "factor": self._factor_dict(factor),
            "signed_at": utc_text(signed_dt),
            "standard_loss_barrels": quantities["standard_loss_barrels"],
            "disputed_loss_barrels": quantities["disputed_loss_barrels"],
        }
        self.connection.execute(
            "INSERT INTO emission_adjustments(adjustment_id,quarter_id,source_quarter_id,kind,transfer_id,"
            "factor_id,delta_barrels,delta_kgco2e,reason,detail_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                adjustment_id,
                target,
                source_quarter_id,
                "late_signoff",
                transfer["transfer_id"],
                factor["factor_id"],
                quantities["signed_barrels"],
                quantities["kgco2e"],
                "迟到签收滚入下一期",
                canonical_json(detail),
                actor_id,
                self._now(),
            ),
        )
        self._audit(
            "emission_adjustment",
            adjustment_id,
            "adjustment.created",
            actor_id,
            {"kind": "late_signoff", "quarter_id": target, "source_quarter_id": source_quarter_id},
        )
        row = self.connection.execute(
            "SELECT * FROM emission_adjustments WHERE adjustment_id=?", (adjustment_id,)
        ).fetchone()
        return self._adjustment_dict(row)

    @staticmethod
    def _adjustment_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "adjustment_id": row["adjustment_id"],
            "kind": row["kind"],
            "quarter_id": row["quarter_id"],
            "source_quarter_id": row["source_quarter_id"],
            "transfer_id": row["transfer_id"],
            "factor_id": row["factor_id"],
            "delta_barrels": row["delta_barrels"],
            "delta_kgco2e": row["delta_kgco2e"],
            "reason": row["reason"],
            "detail": json.loads(row["detail_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def _build_quarter_statement(self, quarter_id: str) -> dict[str, Any]:
        start, end = quarter_bounds(quarter_id)
        rows = self.connection.execute(
            "SELECT t.transfer_id,t.departed_at,t.loaded_barrels,t.expected_delivered_barrels,"
            "s.signed_barrels,s.signed_at,n.route_id,r.product "
            "FROM transfer_signoffs s "
            "JOIN transfers t ON t.transfer_id=s.transfer_id "
            "JOIN nominations n ON n.nomination_id=t.nomination_id "
            "JOIN routes r ON r.route_id=n.route_id"
        ).fetchall()
        versions_cache: dict[tuple[str, str], list[sqlite3.Row]] = {}
        lines: list[dict[str, Any]] = []
        for row in rows:
            signed_dt = parse_utc(row["signed_at"], "signed_at")
            if not start <= signed_dt < end:
                continue
            key = (row["route_id"], row["product"])
            if key not in versions_cache:
                versions_cache[key] = self._factor_versions(*key)
            departed_dt = parse_utc(row["departed_at"], "departed_at")
            factor = resolve_factor(versions_cache[key], departed_dt)
            if factor is None:
                raise InvalidState(
                    f"转运 {row['transfer_id']} 在 {utc_text(departed_dt)} 没有适用的能耗因子版本"
                )
            quantities = transfer_emission(
                loaded=Decimal(row["loaded_barrels"]),
                expected_delivered=Decimal(row["expected_delivered_barrels"]),
                signed=Decimal(row["signed_barrels"]),
                kgco2e_per_barrel=Decimal(factor["kgco2e_per_barrel"]),
            )
            lines.append({
                "transfer_id": row["transfer_id"],
                "route_id": row["route_id"],
                "product": row["product"],
                "departed_at": row["departed_at"],
                "signed_at": row["signed_at"],
                "loaded_barrels": row["loaded_barrels"],
                "expected_delivered_barrels": row["expected_delivered_barrels"],
                **quantities,
                "factor": self._factor_dict(factor),
            })
        excluded_rows = self.connection.execute(
            "SELECT t.transfer_id,t.departed_at,t.state,s.signed_at FROM transfers t "
            "LEFT JOIN transfer_signoffs s ON s.transfer_id=t.transfer_id"
        ).fetchall()
        excluded: list[dict[str, Any]] = []
        for row in excluded_rows:
            departed_dt = parse_utc(row["departed_at"], "departed_at")
            if departed_dt >= end:
                continue
            if row["signed_at"] is not None and parse_utc(row["signed_at"], "signed_at") < end:
                continue
            excluded.append({
                "transfer_id": row["transfer_id"],
                "departed_at": row["departed_at"],
                "state": row["state"],
            })
        adjustments = [
            self._adjustment_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM emission_adjustments WHERE quarter_id=? ORDER BY adjustment_id",
                (quarter_id,),
            ).fetchall()
        ]
        return build_statement(
            quarter_id=quarter_id,
            lines=lines,
            adjustments=adjustments,
            excluded_in_transit=excluded,
        )

    def preview_quarter(self, actor_id: str, quarter_id: str) -> dict[str, Any]:
        self._require(actor_id, "quarter.preview")
        self._valid_quarter(quarter_id)
        if self._quarter_state(quarter_id) == "sealed":
            raise InvalidState("季度已封存，请读取封存报表")
        return {**self._build_quarter_statement(quarter_id), "state": "open"}

    def seal_quarter(self, actor_id: str, quarter_id: str) -> dict[str, Any]:
        self._require(actor_id, "quarter.seal")
        self._valid_quarter(quarter_id)
        if self._quarter_state(quarter_id) == "sealed":
            raise InvalidState("季度已经封存")
        statement = self._build_quarter_statement(quarter_id)
        sealed_at = self._now()
        stored = {**statement, "state": "sealed", "sealed_by": actor_id, "sealed_at": sealed_at}
        factor_ids = {line["factor"]["factor_id"] for line in statement["lines"]}
        for adjustment in statement["adjustments"]:
            if adjustment["factor_id"]:
                factor_ids.add(adjustment["factor_id"])
            detail_factor = adjustment["detail"].get("factor")
            if detail_factor:
                factor_ids.add(detail_factor["factor_id"])
        with transaction(self.connection, immediate=True):
            self._ensure_quarter(quarter_id)
            cursor = self.connection.execute(
                "UPDATE emission_quarters SET state='sealed',sealed_by=?,sealed_at=?,statement_json=?,"
                "statement_sha256=? WHERE quarter_id=? AND state='open'",
                (actor_id, sealed_at, canonical_json(stored), statement["statement_sha256"], quarter_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("季度已经封存")
            for factor_id in sorted(factor_ids):
                self.connection.execute(
                    "INSERT OR IGNORE INTO emission_statement_factors(quarter_id,factor_id) VALUES(?,?)",
                    (quarter_id, factor_id),
                )
            self._audit(
                "emission_quarter",
                quarter_id,
                "quarter.sealed",
                actor_id,
                {"statement_sha256": statement["statement_sha256"]},
            )
        return stored

    def quarter_statement(self, actor_id: str, quarter_id: str) -> dict[str, Any]:
        self._require(actor_id, "emission.read")
        self._valid_quarter(quarter_id)
        row = self.connection.execute(
            "SELECT * FROM emission_quarters WHERE quarter_id=?", (quarter_id,)
        ).fetchone()
        if row is None or row["state"] != "sealed":
            raise InvalidState("季度尚未封存，请使用预览")
        return json.loads(row["statement_json"])

    def create_factor_correction(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "adjustment.write")
        factor_id = identifier(raw.get("factor_id"), "factor_id")
        corrected = decimal_value(
            raw.get("corrected_kgco2e_per_barrel"),
            "corrected_kgco2e_per_barrel",
            minimum=Decimal("0"),
            maximum=Decimal("100000"),
        )
        reason = required_text(raw.get("reason"), "reason")
        factor = self.emission_factor(factor_id)
        refs = self.connection.execute(
            "SELECT q.quarter_id,q.statement_json FROM emission_statement_factors f "
            "JOIN emission_quarters q ON q.quarter_id=f.quarter_id "
            "WHERE f.factor_id=? AND q.state='sealed' ORDER BY q.quarter_id",
            (factor_id,),
        ).fetchall()
        if not refs:
            raise InvalidState("因子未被封存核算引用，可直接更正")
        created: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            for ref in refs:
                statement = json.loads(ref["statement_json"])
                affected = [
                    line for line in statement["lines"] if line["factor"]["factor_id"] == factor_id
                ]
                if not affected:
                    continue
                delta = sum(
                    (
                        DEFAULT_PRECISION.emission(
                            (corrected - Decimal(line["factor"]["kgco2e_per_barrel"]))
                            * Decimal(line["signed_barrels"])
                        )
                        for line in affected
                    ),
                    Decimal("0"),
                )
                delta = DEFAULT_PRECISION.emission(delta)
                if delta == Decimal("0"):
                    continue
                source_quarter_id = ref["quarter_id"]
                target = self._earliest_open_quarter_after(source_quarter_id)
                self._ensure_quarter(target)
                sequence = self.connection.execute(
                    "SELECT count(*) FROM emission_adjustments WHERE kind='factor_correction' "
                    "AND factor_id=? AND source_quarter_id=?",
                    (factor_id, source_quarter_id),
                ).fetchone()[0]
                adjustment_id = f"adj-fc-{factor_id}-{source_quarter_id}"
                if sequence:
                    adjustment_id = f"{adjustment_id}-{sequence + 1}"
                affected_barrels = sum((Decimal(line["signed_barrels"]) for line in affected), Decimal("0"))
                detail = {
                    "original_kgco2e_per_barrel": factor["kgco2e_per_barrel"],
                    "corrected_kgco2e_per_barrel": decimal_text(corrected),
                    "affected_transfer_count": len(affected),
                    "affected_signed_barrels": decimal_text(DEFAULT_PRECISION.volume(affected_barrels)),
                }
                self.connection.execute(
                    "INSERT INTO emission_adjustments(adjustment_id,quarter_id,source_quarter_id,kind,"
                    "transfer_id,factor_id,delta_barrels,delta_kgco2e,reason,detail_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        adjustment_id,
                        target,
                        source_quarter_id,
                        "factor_correction",
                        None,
                        factor_id,
                        decimal_text(DEFAULT_PRECISION.volume(Decimal("0"))),
                        decimal_text(delta),
                        reason,
                        canonical_json(detail),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "emission_adjustment",
                    adjustment_id,
                    "adjustment.created",
                    actor_id,
                    {"kind": "factor_correction", "quarter_id": target, "source_quarter_id": source_quarter_id},
                )
                created.append(
                    self._adjustment_dict(
                        self.connection.execute(
                            "SELECT * FROM emission_adjustments WHERE adjustment_id=?", (adjustment_id,)
                        ).fetchone()
                    )
                )
        return {"factor_id": factor_id, "adjustments": created}

    def export_quarter(self, actor_id: str, quarter_id: str) -> dict[str, Any]:
        self._require(actor_id, "emission.read")
        self._valid_quarter(quarter_id)
        row = self.connection.execute(
            "SELECT statement_json,state FROM emission_quarters WHERE quarter_id=?", (quarter_id,)
        ).fetchone()
        if row is not None and row["state"] == "sealed":
            return export_document(json.loads(row["statement_json"]), state="sealed")
        return export_document(self._build_quarter_statement(quarter_id), state="open")
