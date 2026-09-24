from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from oil_supply import emissions_cli
from oil_supply.acceptance import run as acceptance_run
from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.emissions import EmissionsService, next_quarter, quarter_of, validate_quarter
from oil_supply.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from oil_supply.planning import canonical_json
from oil_supply.service import SupplyService
from oil_supply.storage import connect


ROOT = Path(__file__).resolve().parents[1]


def prepare(connection: sqlite3.Connection, clock: FrozenClock) -> tuple[SupplyService, EmissionsService]:
    supply = SupplyService(connection, clock)
    emissions = EmissionsService(connection, clock)
    for user_id, role in (
        ("plan", "planner"),
        ("dispatch", "dispatcher"),
        ("risk", "risk"),
        ("audit", "auditor"),
        ("carbon", "emissions"),
    ):
        supply.create_user(user_id, user_id, role)
    supply.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    supply.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    supply.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    supply.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "1000000", "unit_cost_usd": "91", "received_at": "2026-01-01T00:00:00Z"})
    emissions.register_factor("carbon", {"route_id": "pipe-a-b", "product": "crude", "retrofit_stage": "pre_retrofit", "factor_value": "0.42", "effective_from": "2026-01-01T00:00:00Z"})
    emissions.register_factor("carbon", {"route_id": "pipe-a-b", "product": "crude", "retrofit_stage": "post_retrofit", "factor_value": "0.36", "effective_from": "2026-09-01T00:00:00Z"})
    return supply, emissions


def dispatch(supply: SupplyService, transfer_id: str, service_date: str, barrels: str = "80000") -> str:
    supply.submit_nomination("dispatch", {"nomination_id": f"nom-{transfer_id}", "route_id": "pipe-a-b", "shipper_id": "refinery", "service_date": service_date, "requested_barrels": barrels, "priority": 10, "idempotency_key": f"key-{transfer_id}"})
    supply.allocate("dispatch", "pipe-a-b", service_date)
    supply.dispatch_transfer("dispatch", transfer_id, f"nom-{transfer_id}", "lot-1", 2)
    return transfer_id


class QuarterHelperTests(unittest.TestCase):
    def test_quarter_helpers(self) -> None:
        self.assertEqual(quarter_of(datetime(2026, 1, 1, tzinfo=timezone.utc)), "2026Q1")
        self.assertEqual(quarter_of(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)), "2026Q4")
        self.assertEqual(next_quarter("2026Q3"), "2026Q4")
        self.assertEqual(next_quarter("2026Q4"), "2027Q1")
        with self.assertRaises(ValidationFailed):
            validate_quarter("2026-Q1")


class EmissionsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.supply, self.emissions = prepare(self.connection, self.clock)

    def tearDown(self) -> None:
        self.connection.close()

    def receipt(self, transfer_id: str, signed: str, signed_at: str) -> dict[str, object]:
        return self.emissions.record_receipt(
            "carbon", {"transfer_id": transfer_id, "signed_barrels": signed, "signed_at": signed_at}
        )

    def test_factor_versions_form_non_overlapping_windows(self) -> None:
        listing = self.emissions.list_factors("audit", "pipe-a-b", "crude")
        self.assertEqual(len(listing["versions"]), 2)
        pre, post = listing["versions"]
        self.assertEqual(pre["retrofit_stage"], "pre_retrofit")
        self.assertEqual(pre["effective_to"], "2026-09-01T00:00:00Z")
        self.assertIsNone(post["effective_to"])
        self.assertFalse(pre["referenced"])
        with self.assertRaises(Conflict):
            self.emissions.register_factor("carbon", {"route_id": "pipe-a-b", "product": "crude", "retrofit_stage": "pre_retrofit", "factor_value": "0.5", "effective_from": "2026-01-01T00:00:00Z"})

    def test_receipt_resolves_factor_at_switch_boundary(self) -> None:
        self.clock.current = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
        dispatch(self.supply, "t-bound-1", "2026-08-30")
        dispatch(self.supply, "t-bound-2", "2026-08-31")
        self.clock.current = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        before = self.receipt("t-bound-1", "79700", "2026-08-31T23:59:59Z")
        after = self.receipt("t-bound-2", "79700", "2026-09-01T00:00:00Z")
        self.assertEqual(before["factor"]["retrofit_stage"], "pre_retrofit")
        self.assertEqual(before["factor"]["factor_value"], "0.420000")
        self.assertEqual(before["emissions_kg"], "33474.000")
        self.assertEqual(after["factor"]["retrofit_stage"], "post_retrofit")
        self.assertEqual(after["factor"]["factor_value"], "0.360000")
        self.assertEqual(after["emissions_kg"], "28692.000")

    def test_receipt_uses_signed_quantity_and_splits_losses(self) -> None:
        dispatch(self.supply, "t-1", "2026-09-25")
        self.clock.advance(hours=40)
        entry = self.receipt("t-1", "79700", "2026-09-26T00:00:00Z")
        self.assertEqual(entry["signed_barrels"], "79700.000")
        self.assertEqual(entry["standard_loss_barrels"], "200.000")
        self.assertEqual(entry["disputed_loss_barrels"], "100.000")
        # 只认签收数量：不是 79800（预期）也不是 80000（装船）对应的排放量
        self.assertEqual(entry["emissions_kg"], "28692.000")
        self.assertEqual(entry["precision"]["rounding"], "ROUND_HALF_UP")
        transfer = self.connection.execute("SELECT state FROM transfers WHERE transfer_id='t-1'").fetchone()
        self.assertEqual(transfer["state"], "disputed")

    def test_unreferenced_factor_can_be_retired_but_referenced_is_immutable(self) -> None:
        listing = self.emissions.list_factors("audit", "pipe-a-b", "crude")
        post_id = listing["versions"][1]["factor_id"]
        result = self.emissions.retire_factor("carbon", post_id)
        self.assertTrue(result["retired"])
        self.assertEqual(len(self.emissions.list_factors("audit", "pipe-a-b", "crude")["versions"]), 1)
        replacement = self.emissions.register_factor("carbon", {"route_id": "pipe-a-b", "product": "crude", "retrofit_stage": "post_retrofit", "factor_value": "0.36", "effective_from": "2026-09-01T00:00:00Z"})
        dispatch(self.supply, "t-1", "2026-09-25")
        self.clock.advance(hours=40)
        self.receipt("t-1", "79700", "2026-09-26T00:00:00Z")
        with self.assertRaises(Conflict):
            self.emissions.retire_factor("carbon", replacement["factor_id"])
        pre_id = self.emissions.list_factors("audit", "pipe-a-b", "crude")["versions"][0]["factor_id"]
        self.assertTrue(self.emissions.retire_factor("carbon", pre_id)["retired"])

    def test_history_is_not_recalculated_when_new_factor_appears(self) -> None:
        self.clock.current = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)
        dispatch(self.supply, "t-1", "2026-09-09")
        dispatch(self.supply, "t-2", "2026-09-10")
        self.clock.current = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)
        first = self.receipt("t-1", "79700", "2026-09-10T00:00:00Z")
        self.assertEqual(first["emissions_kg"], "28692.000")
        self.emissions.register_factor("carbon", {"route_id": "pipe-a-b", "product": "crude", "retrofit_stage": "post_retrofit", "factor_value": "0.3", "effective_from": "2026-09-05T00:00:00Z"})
        second = self.receipt("t-2", "79700", "2026-09-12T00:00:00Z")
        self.assertEqual(second["factor"]["factor_value"], "0.300000")
        self.assertEqual(second["emissions_kg"], "23910.000")
        reloaded = self.emissions.entry_for_transfer("audit", "t-1")
        self.assertEqual(reloaded["factor"]["factor_value"], "0.360000")
        self.assertEqual(reloaded["emissions_kg"], "28692.000")
        statement = self.emissions.statement("carbon", "2026Q3")
        self.assertEqual(statement["totals"]["grand"]["emissions_kg"], "52602.000")

    def test_open_preview_restricted_and_seal_freezes_statement(self) -> None:
        dispatch(self.supply, "t-1", "2026-09-25")
        self.clock.advance(hours=40)
        self.receipt("t-1", "79700", "2026-09-26T00:00:00Z")
        with self.assertRaises(Forbidden):
            self.emissions.statement("audit", "2026Q3")
        preview = self.emissions.statement("carbon", "2026Q3")
        self.assertEqual(preview["state"], "open")
        with self.assertRaises(Forbidden):
            self.emissions.seal_statement("audit", "2026Q3")
        sealed = self.emissions.seal_statement("carbon", "2026Q3")
        self.assertEqual(sealed["state"], "sealed")
        self.assertTrue(sealed["integrity_valid"])
        self.assertEqual(len(sealed["content_sha256"]), 64)
        with self.assertRaises(InvalidState):
            self.emissions.seal_statement("carbon", "2026Q3")
        view = self.emissions.statement("audit", "2026Q3")
        self.assertTrue(view["integrity_valid"])
        row = self.connection.execute("SELECT result_json FROM emission_statements WHERE quarter='2026Q3'").fetchone()
        payload = json.loads(row["result_json"])
        payload["totals"]["grand"]["emissions_kg"] = "0.001"
        self.connection.execute("UPDATE emission_statements SET result_json=? WHERE quarter='2026Q3'", (json.dumps(payload),))
        self.assertFalse(self.emissions.statement("audit", "2026Q3")["integrity_valid"])

    def test_late_receipt_after_sealing_rolls_into_next_quarter(self) -> None:
        self.clock.current = datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)
        dispatch(self.supply, "t-1", "2026-06-20")
        dispatch(self.supply, "t-2", "2026-06-21")
        self.clock.current = datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)
        self.receipt("t-1", "79700", "2026-06-25T00:00:00Z")
        sealed = self.emissions.seal_statement("carbon", "2026Q2")
        self.clock.current = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(InvalidState):
            self.receipt("t-2", "79000", "2026-06-28T00:00:00Z")
        opened = self.emissions.open_adjustment("carbon", {"adjustment_type": "late_receipt", "transfer_id": "t-2", "signed_barrels": "79000", "signed_at": "2026-06-28T00:00:00Z", "reason": "签收单迟到"})
        adjustment = opened["adjustment"]
        self.assertEqual(adjustment["source_quarter"], "2026Q2")
        self.assertEqual(adjustment["target_quarter"], "2026Q3")
        self.assertEqual(adjustment["delta_emissions_kg"], "33180.000")
        self.assertEqual(opened["entry"]["quarter"], "2026Q2")
        self.assertEqual(opened["entry"]["adjustment_id"], adjustment["adjustment_id"])
        third = self.emissions.statement("carbon", "2026Q3")
        self.assertEqual(third["totals"]["base"]["entries"], 0)
        self.assertEqual(len(third["adjustments"]), 1)
        self.assertEqual(third["totals"]["grand"]["emissions_kg"], "33180.000")
        frozen = self.emissions.statement("audit", "2026Q2")
        self.assertEqual(frozen["content_sha256"], sealed["content_sha256"])
        self.assertEqual(frozen["totals"]["grand"]["emissions_kg"], "33474.000")
        dispatch(self.supply, "t-3", "2026-07-05")
        with self.assertRaises(InvalidState):
            self.emissions.open_adjustment("carbon", {"adjustment_type": "late_receipt", "transfer_id": "t-3", "signed_barrels": "79000", "signed_at": "2026-07-05T00:00:00Z", "reason": "季度未封存"})

    def test_factor_correction_after_sealing_books_delta_once(self) -> None:
        self.clock.current = datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)
        dispatch(self.supply, "t-1", "2026-06-20")
        self.clock.current = datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)
        self.receipt("t-1", "79000", "2026-06-25T00:00:00Z")
        sealed = self.emissions.seal_statement("carbon", "2026Q2")
        self.emissions.register_factor("carbon", {"route_id": "pipe-a-b", "product": "crude", "retrofit_stage": "pre_retrofit", "factor_value": "0.4", "effective_from": "2026-04-01T00:00:00Z"})
        opened = self.emissions.open_adjustment("carbon", {"adjustment_type": "factor_correction", "source_quarter": "2026Q2", "route_id": "pipe-a-b", "product": "crude", "reason": "因子修正"})
        adjustment = opened["adjustment"]
        self.assertEqual(adjustment["target_quarter"], "2026Q3")
        self.assertEqual(adjustment["delta_emissions_kg"], "-1580.000")
        with self.assertRaises(InvalidState):
            self.emissions.open_adjustment("carbon", {"adjustment_type": "factor_correction", "source_quarter": "2026Q2", "route_id": "pipe-a-b", "product": "crude", "reason": "重复"})
        third = self.emissions.statement("carbon", "2026Q3")
        self.assertEqual(third["totals"]["adjustments"]["emissions_kg"], "-1580.000")
        frozen = self.emissions.statement("audit", "2026Q2")
        self.assertEqual(frozen["content_sha256"], sealed["content_sha256"])
        self.assertEqual(frozen["totals"]["grand"]["emissions_kg"], "33180.000")
        with self.assertRaises(InvalidState):
            self.emissions.open_adjustment("carbon", {"adjustment_type": "factor_correction", "source_quarter": "2026Q3", "route_id": "pipe-a-b", "product": "crude", "reason": "未封存"})
        self.emissions.seal_statement("carbon", "2026Q1")
        with self.assertRaises(InvalidState):
            self.emissions.open_adjustment("carbon", {"adjustment_type": "factor_correction", "source_quarter": "2026Q1", "route_id": "pipe-a-b", "product": "crude", "reason": "没有条目"})

    def test_zero_transport_quarter_seals_with_explicit_zeros(self) -> None:
        sealed = self.emissions.seal_statement("carbon", "2025Q4")
        self.assertEqual(sealed["state"], "sealed")
        self.assertEqual(sealed["entries"], [])
        self.assertEqual(sealed["adjustments"], [])
        self.assertEqual(sealed["totals"]["grand"]["emissions_kg"], "0.000")
        self.assertEqual(sealed["totals"]["grand"]["signed_barrels"], "0.000")
        self.assertEqual(len(sealed["content_sha256"]), 64)

    def test_cross_period_in_transit_is_visible_then_counted_when_signed(self) -> None:
        self.clock.current = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
        dispatch(self.supply, "t-1", "2026-09-20")
        preview = self.emissions.statement("carbon", "2026Q3")
        self.assertEqual([row["transfer_id"] for row in preview["in_transit"]], ["t-1"])
        self.assertEqual(preview["entries"], [])
        self.assertEqual(preview["totals"]["grand"]["emissions_kg"], "0.000")
        self.clock.current = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        entry = self.receipt("t-1", "79000", "2026-10-02T01:00:00Z")
        self.assertEqual(entry["quarter"], "2026Q4")
        third = self.emissions.statement("carbon", "2026Q3")
        self.assertEqual(third["in_transit"], [])
        self.assertEqual(third["entries"], [])
        fourth = self.emissions.statement("carbon", "2026Q4")
        self.assertEqual([row["transfer_id"] for row in fourth["entries"]], ["t-1"])
        self.assertEqual(fourth["totals"]["grand"]["emissions_kg"], "28440.000")

    def test_export_is_byte_stable_and_orders_details(self) -> None:
        dispatch(self.supply, "t-b", "2026-09-24")
        dispatch(self.supply, "t-a", "2026-09-25")
        self.clock.current = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
        self.receipt("t-a", "79700", "2026-09-26T00:00:00Z")
        self.receipt("t-b", "79700", "2026-09-25T23:00:00Z")
        self.emissions.seal_statement("carbon", "2026Q3")
        first = self.emissions.export_quarter("audit", "2026Q3")
        second = self.emissions.export_quarter("audit", "2026Q3")
        self.assertEqual(first, second)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual([row["transfer_id"] for row in first["entries"]], ["t-b", "t-a"])
        open_export_one = self.emissions.export_quarter("carbon", "2026Q4")
        open_export_two = self.emissions.export_quarter("carbon", "2026Q4")
        self.assertEqual(canonical_json(open_export_one), canonical_json(open_export_two))

    def test_receipt_validation_and_idempotent_replay(self) -> None:
        dispatch(self.supply, "t-1", "2026-09-25")
        with self.assertRaises(ValidationFailed):
            self.receipt("t-1", "80000.001", "2026-09-24T08:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.receipt("t-1", "-1", "2026-09-24T08:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.receipt("t-1", "79700", "2026-09-24T09:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.receipt("t-1", "79700", "2026-09-24T07:00:00Z")
        self.clock.advance(hours=40)
        created = self.receipt("t-1", "79700", "2026-09-26T00:00:00Z")
        replayed = self.receipt("t-1", "79700", "2026-09-26T00:00:00Z")
        self.assertEqual(created["entry_id"], replayed["entry_id"])
        with self.assertRaises(Conflict):
            self.receipt("t-1", "79701", "2026-09-26T00:00:00Z")
        with self.assertRaises(NotFound):
            self.receipt("t-unknown", "79700", "2026-09-26T00:00:00Z")
        self.clock.current = datetime(2025, 12, 1, 0, 0, tzinfo=timezone.utc)
        dispatch(self.supply, "t-9", "2025-12-01")
        self.clock.current = datetime(2025, 12, 3, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(InvalidState):
            self.receipt("t-9", "79700", "2025-12-02T00:00:00Z")

    def test_api_endpoints(self) -> None:
        app = JsonApplication(self.supply)
        created = app.handle("POST", "/emissions/factors", {"X-Actor-Id": "carbon"}, json.dumps({"route_id": "pipe-a-b", "product": "crude", "retrofit_stage": "post_retrofit", "factor_value": "0.3", "effective_from": "2026-09-10T00:00:00Z"}).encode())
        self.assertEqual(created.status, 201)
        listing = app.handle("GET", "/emissions/factors?route_id=pipe-a-b&product=crude", {"X-Actor-Id": "audit"})
        self.assertEqual(listing.status, 200)
        self.assertEqual(len(listing.body["versions"]), 3)
        dispatch(self.supply, "t-1", "2026-09-25")
        self.clock.advance(hours=40)
        receipt = app.handle("POST", "/emissions/receipts", {"X-Actor-Id": "carbon"}, json.dumps({"transfer_id": "t-1", "signed_barrels": "79700", "signed_at": "2026-09-26T00:00:00Z"}).encode())
        self.assertEqual(receipt.status, 201)
        denied = app.handle("GET", "/emissions/statements/2026Q3", {"X-Actor-Id": "audit"})
        self.assertEqual(denied.status, 403)
        sealed = app.handle("POST", "/emissions/statements/2026Q3/seal", {"X-Actor-Id": "carbon"}, b"{}")
        self.assertEqual(sealed.status, 200)
        self.assertEqual(sealed.body["state"], "sealed")
        entry = app.handle("GET", "/emissions/entries/t-1", {"X-Actor-Id": "audit"})
        self.assertEqual(entry.status, 200)
        self.assertEqual(entry.body["transfer_id"], "t-1")
        statement = app.handle("GET", "/emissions/statements/2026Q3", {"X-Actor-Id": "audit"})
        self.assertEqual(statement.status, 200)
        self.assertTrue(statement.body["integrity_valid"])


class EmissionsCliTests(unittest.TestCase):
    def test_cli_export_is_stable_and_reports_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "emissions.sqlite3"
            connection = connect(database)
            clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
            supply, emissions = prepare(connection, clock)
            dispatch(supply, "t-1", "2026-09-25")
            clock.advance(hours=40)
            emissions.record_receipt("carbon", {"transfer_id": "t-1", "signed_barrels": "79700", "signed_at": "2026-09-26T00:00:00Z"})
            emissions.seal_statement("carbon", "2026Q3")
            connection.close()

            outputs: list[str] = []
            for _ in range(2):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    exit_code = emissions_cli.main(["--database", str(database), "--actor", "audit", "--quarter", "2026Q3"])
                self.assertEqual(exit_code, 0)
                outputs.append(buffer.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            payload = json.loads(outputs[0])
            self.assertEqual(payload["state"], "sealed")
            self.assertEqual(len(payload["content_sha256"]), 64)
            self.assertEqual([row["transfer_id"] for row in payload["entries"]], ["t-1"])
            self.assertEqual(payload["totals"]["grand"]["emissions_kg"], "28692.000")

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                bad = emissions_cli.main(["--database", str(database), "--actor", "audit", "--quarter", "2026-Q3"])
            self.assertEqual(bad, 1)
            denied = emissions_cli.main(["--database", str(database), "--actor", "audit", "--quarter", "2026Q4"])
            self.assertEqual(denied, 1)


class EmissionsAcceptanceTests(unittest.TestCase):
    def test_acceptance_run_includes_emissions(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        emissions = result["emissions"]
        self.assertEqual(emissions["sealed_quarter"], "2026Q3")
        self.assertEqual(emissions["entry"]["factor"]["retrofit_stage"], "post_retrofit")
        self.assertEqual(emissions["sealed_totals"]["emissions_kg"], "28692.000")
        self.assertEqual(emissions["empty_quarter_totals"]["emissions_kg"], "0.000")
        self.assertEqual(len(emissions["export_sha256"]), 64)
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
