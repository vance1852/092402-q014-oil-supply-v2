from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from oil_supply import emissions_cli
from oil_supply.api import JsonApplication
from oil_supply.clock import FrozenClock
from oil_supply.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from oil_supply.service import SupplyService
from oil_supply.storage import connect


class EmissionsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("dispatch", "dispatcher"),
            ("risk", "risk"),
            ("audit", "auditor"),
            ("emis", "emissions_officer"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
        self.service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
        self.service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def set_clock(self, text: str) -> None:
        self.clock.current = datetime.fromisoformat(text.replace("Z", "+00:00"))

    def register_factors(self, switch: str = "2026-09-01T00:00:00Z") -> None:
        self.service.register_emission_factor("emis", {"factor_id": "ef-pre", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "14.5", "equipment_generation": "pre_retrofit", "effective_from": "2026-01-01T00:00:00Z", "effective_to": switch})
        self.service.register_emission_factor("emis", {"factor_id": "ef-post", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "11.2", "equipment_generation": "post_retrofit", "effective_from": switch})

    def nominate(self, tag: str, service_date: str, requested: str = "40000") -> None:
        self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{tag}", "route_id": "pipe-a-b", "shipper_id": "refinery", "service_date": service_date, "requested_barrels": requested, "priority": 10, "idempotency_key": f"key-{tag}"})

    def dispatch(self, tag: str, departed: str) -> None:
        self.set_clock(departed)
        self.service.add_inventory_lot("dispatch", {"lot_id": f"lot-{tag}", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "60000", "unit_cost_usd": "90", "received_at": "2026-01-01T00:00:00Z"})
        self.service.dispatch_transfer("dispatch", f"tr-{tag}", f"nom-{tag}", f"lot-{tag}", 2)

    def sign(self, tag: str, signed: str, signed_at: str) -> dict[str, object]:
        self.set_clock(signed_at)
        return self.service.sign_transfer("dispatch", f"tr-{tag}", signed, signed_at)

    def make_transfer(self, tag: str, *, service_date: str, departed: str, signed: str | None = None, signed_at: str | None = None) -> None:
        self.nominate(tag, service_date)
        self.service.allocate("dispatch", "pipe-a-b", service_date)
        self.dispatch(tag, departed)
        if signed is not None:
            self.sign(tag, signed, signed_at)


class FactorVersionTests(EmissionsTestBase):
    def test_versions_must_not_overlap_but_may_touch(self) -> None:
        self.register_factors()
        with self.assertRaises(Conflict):
            self.service.register_emission_factor("emis", {"factor_id": "ef-x", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "10", "equipment_generation": "post_retrofit", "effective_from": "2026-06-01T00:00:00Z", "effective_to": "2026-07-01T00:00:00Z"})
        adjacent = self.service.register_emission_factor("emis", {"factor_id": "ef-old", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "16", "equipment_generation": "pre_retrofit", "effective_from": "2025-01-01T00:00:00Z", "effective_to": "2026-01-01T00:00:00Z"})
        self.assertEqual(adjacent["factor_id"], "ef-old")
        versions = self.service.list_emission_factors("audit", "pipe-a-b", "crude")
        self.assertEqual([item["factor_id"] for item in versions["versions"]], ["ef-old", "ef-pre", "ef-post"])

    def test_factor_product_must_match_route(self) -> None:
        with self.assertRaises(Conflict):
            self.service.register_emission_factor("emis", {"factor_id": "ef-bad", "route_id": "pipe-a-b", "product": "diesel", "kgco2e_per_barrel": "10", "equipment_generation": "pre_retrofit", "effective_from": "2026-01-01T00:00:00Z"})
        with self.assertRaises(NotFound):
            self.service.register_emission_factor("emis", {"factor_id": "ef-none", "route_id": "pipe-x", "product": "crude", "kgco2e_per_barrel": "10", "equipment_generation": "pre_retrofit", "effective_from": "2026-01-01T00:00:00Z"})

    def test_correction_allowed_before_seal_and_shapes_preview(self) -> None:
        self.register_factors()
        corrected = self.service.correct_emission_factor("emis", "ef-post", {"kgco2e_per_barrel": "11.8"})
        self.assertEqual(corrected["kgco2e_per_barrel"], "11.8")
        self.assertEqual(corrected["revision"], 2)
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        preview = self.service.preview_quarter("emis", "2026Q3")
        self.assertEqual(preview["lines"][0]["kgco2e"], "472000.000")

    def test_referenced_factor_value_is_immutable_but_span_may_close(self) -> None:
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        self.set_clock("2026-10-01T08:00:00Z")
        sealed = self.service.seal_quarter("emis", "2026Q3")
        with self.assertRaises(Conflict):
            self.service.correct_emission_factor("emis", "ef-post", {"kgco2e_per_barrel": "9.0"})
        with self.assertRaises(Conflict):
            self.service.correct_emission_factor("emis", "ef-post", {"effective_from": "2026-08-01T00:00:00Z"})
        closed = self.service.correct_emission_factor("emis", "ef-post", {"effective_to": "2026-10-15T00:00:00Z"})
        self.assertEqual(closed["effective_to"], "2026-10-15T00:00:00Z")
        self.service.register_emission_factor("emis", {"factor_id": "ef-post2", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "9.9", "equipment_generation": "post_retrofit", "effective_from": "2026-10-15T00:00:00Z"})
        statement = self.service.quarter_statement("audit", "2026Q3")
        self.assertEqual(statement["statement_sha256"], sealed["statement_sha256"])
        self.assertEqual(statement["totals"]["total_kgco2e"], "448000.000")
        self.assertIsNone(statement["factor_snapshot"][0]["effective_to"])


class SignoffAndLineTests(EmissionsTestBase):
    def test_switchover_boundary_uses_factor_at_departure(self) -> None:
        self.register_factors()
        self.nominate("a", "2026-08-31")
        self.service.allocate("dispatch", "pipe-a-b", "2026-08-31")
        self.nominate("b", "2026-09-01")
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-01")
        self.dispatch("a", "2026-08-31T23:59:59Z")
        self.dispatch("b", "2026-09-01T00:00:00Z")
        self.sign("a", "40000", "2026-09-02T10:00:00Z")
        self.sign("b", "40000", "2026-09-03T10:00:00Z")
        preview = self.service.preview_quarter("emis", "2026Q3")
        lines = {line["transfer_id"]: line for line in preview["lines"]}
        self.assertEqual(lines["tr-a"]["factor"]["equipment_generation"], "pre_retrofit")
        self.assertEqual(lines["tr-a"]["kgco2e"], "580000.000")
        self.assertEqual(lines["tr-b"]["factor"]["equipment_generation"], "post_retrofit")
        self.assertEqual(lines["tr-b"]["kgco2e"], "448000.000")
        self.assertEqual(preview["totals"]["total_kgco2e"], "1028000.000")

    def test_signed_quantity_drives_emissions_and_losses_are_split(self) -> None:
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="39850", signed_at="2026-09-12T08:00:00Z")
        preview = self.service.preview_quarter("emis", "2026Q3")
        line = preview["lines"][0]
        self.assertEqual(line["loaded_barrels"], "40000.000")
        self.assertEqual(line["expected_delivered_barrels"], "39900.000")
        self.assertEqual(line["signed_barrels"], "39850.000")
        self.assertEqual(line["standard_loss_barrels"], "100.000")
        self.assertEqual(line["disputed_loss_barrels"], "50.000")
        self.assertEqual(line["kgco2e"], "446320.000")
        totals = preview["totals"]
        self.assertEqual(totals["signed_barrels"], "39850.000")
        self.assertEqual(totals["standard_loss_barrels"], "100.000")
        self.assertEqual(totals["disputed_loss_barrels"], "50.000")
        nomination = self.connection.execute("SELECT state,delivered_barrels FROM nominations WHERE nomination_id='nom-a'").fetchone()
        self.assertEqual(nomination["state"], "delivered")
        self.assertEqual(nomination["delivered_barrels"], "39850.000")

    def test_signoff_validation_boundaries(self) -> None:
        self.register_factors()
        self.nominate("a", "2026-09-10")
        self.service.allocate("dispatch", "pipe-a-b", "2026-09-10")
        self.dispatch("a", "2026-09-10T08:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.sign("a", "40000", "2026-09-10T07:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.sign("a", "41000", "2026-09-11T08:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.sign("a", "-1", "2026-09-11T08:00:00Z")
        self.sign("a", "40000", "2026-09-11T08:00:00Z")
        with self.assertRaises(InvalidState):
            self.sign("a", "40000", "2026-09-11T09:00:00Z")
        with self.assertRaises(NotFound):
            self.service.sign_transfer("dispatch", "tr-missing", "1", "2026-09-11T09:00:00Z")

    def test_missing_factor_coverage_fails_clearly(self) -> None:
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        with self.assertRaises(InvalidState):
            self.service.preview_quarter("emis", "2026Q3")


class QuarterLifecycleTests(EmissionsTestBase):
    def test_cross_period_in_transit_is_explicit(self) -> None:
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-28", departed="2026-09-28T08:00:00Z")
        third = self.service.preview_quarter("emis", "2026Q3")
        self.assertEqual(third["lines"], [])
        self.assertEqual(third["totals"]["total_kgco2e"], "0.000")
        self.assertEqual([item["transfer_id"] for item in third["excluded_in_transit"]], ["tr-a"])
        self.sign("a", "39900", "2026-10-02T08:00:00Z")
        fourth = self.service.preview_quarter("emis", "2026Q4")
        self.assertEqual([line["transfer_id"] for line in fourth["lines"]], ["tr-a"])
        self.assertEqual(fourth["totals"]["total_kgco2e"], "446880.000")
        third_after = self.service.preview_quarter("emis", "2026Q3")
        self.assertEqual(third_after["lines"], [])
        self.assertEqual(len(third_after["excluded_in_transit"]), 1)

    def test_zero_transport_quarter_has_clear_result(self) -> None:
        preview = self.service.preview_quarter("emis", "2027Q1")
        self.assertEqual(preview["state"], "open")
        self.assertEqual(preview["lines"], [])
        self.assertEqual(preview["adjustments"], [])
        totals = preview["totals"]
        self.assertEqual(totals["transfer_count"], 0)
        self.assertEqual(totals["signed_barrels"], "0.000")
        self.assertEqual(totals["total_kgco2e"], "0.000")
        sealed = self.service.seal_quarter("emis", "2027Q1")
        self.assertEqual(sealed["state"], "sealed")
        self.assertEqual(sealed["totals"]["total_kgco2e"], "0.000")

    def test_preview_and_statement_state_guards(self) -> None:
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        self.service.seal_quarter("emis", "2026Q3")
        with self.assertRaises(InvalidState):
            self.service.preview_quarter("emis", "2026Q3")
        with self.assertRaises(InvalidState):
            self.service.seal_quarter("emis", "2026Q3")
        with self.assertRaises(InvalidState):
            self.service.quarter_statement("audit", "2027Q1")
        with self.assertRaises(ValidationFailed):
            self.service.preview_quarter("emis", "2026-Q3")

    def test_late_signoff_rolls_into_next_open_quarter(self) -> None:
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        self.make_transfer("b", service_date="2026-09-20", departed="2026-09-20T08:00:00Z")
        self.set_clock("2026-10-01T08:00:00Z")
        sealed = self.service.seal_quarter("emis", "2026Q3")
        self.assertEqual(len(sealed["lines"]), 1)
        self.assertEqual([item["transfer_id"] for item in sealed["excluded_in_transit"]], ["tr-b"])
        result = self.sign("b", "39900", "2026-09-25T08:00:00Z")
        self.assertEqual(result["quarter_id"], "2026Q3")
        adjustment = result["adjustment"]
        self.assertEqual(adjustment["kind"], "late_signoff")
        self.assertEqual(adjustment["quarter_id"], "2026Q4")
        self.assertEqual(adjustment["source_quarter_id"], "2026Q3")
        self.assertEqual(adjustment["delta_kgco2e"], "446880.000")
        statement = self.service.quarter_statement("audit", "2026Q3")
        self.assertEqual(statement["statement_sha256"], sealed["statement_sha256"])
        self.assertEqual(len(statement["lines"]), 1)
        fourth = self.service.preview_quarter("emis", "2026Q4")
        self.assertEqual(fourth["lines"], [])
        self.assertEqual(fourth["totals"]["adjustments_kgco2e"], "446880.000")
        self.assertEqual(fourth["totals"]["total_kgco2e"], "446880.000")

    def test_adjustment_skips_already_sealed_quarters(self) -> None:
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-20", departed="2026-09-20T08:00:00Z")
        self.set_clock("2026-10-02T08:00:00Z")
        self.service.seal_quarter("emis", "2026Q3")
        self.service.seal_quarter("emis", "2026Q4")
        result = self.sign("a", "40000", "2026-09-25T08:00:00Z")
        self.assertEqual(result["adjustment"]["quarter_id"], "2027Q1")

    def test_factor_correction_rolls_delta_into_next_quarter(self) -> None:
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        self.set_clock("2026-10-01T08:00:00Z")
        self.service.seal_quarter("emis", "2026Q3")
        result = self.service.create_factor_correction("emis", {"factor_id": "ef-post", "corrected_kgco2e_per_barrel": "12.0", "reason": "计量复核后修正"})
        self.assertEqual(len(result["adjustments"]), 1)
        adjustment = result["adjustments"][0]
        self.assertEqual(adjustment["kind"], "factor_correction")
        self.assertEqual(adjustment["quarter_id"], "2026Q4")
        self.assertEqual(adjustment["delta_kgco2e"], "32000.000")
        self.assertEqual(adjustment["delta_barrels"], "0.000")
        fourth = self.service.preview_quarter("emis", "2026Q4")
        self.assertEqual(fourth["totals"]["adjustments_kgco2e"], "32000.000")
        statement = self.service.quarter_statement("audit", "2026Q3")
        self.assertEqual(statement["totals"]["total_kgco2e"], "448000.000")
        self.assertEqual(statement["factor_snapshot"][0]["kgco2e_per_barrel"], "11.2")
        again = self.service.create_factor_correction("emis", {"factor_id": "ef-post", "corrected_kgco2e_per_barrel": "12.5", "reason": "再次修正"})
        self.assertNotEqual(again["adjustments"][0]["adjustment_id"], adjustment["adjustment_id"])
        self.assertEqual(again["adjustments"][0]["delta_kgco2e"], "52000.000")

    def test_factor_correction_requires_sealed_reference(self) -> None:
        self.register_factors()
        with self.assertRaises(InvalidState):
            self.service.create_factor_correction("emis", {"factor_id": "ef-post", "corrected_kgco2e_per_barrel": "12.0", "reason": "过早"})


class ExportAndPermissionTests(EmissionsTestBase):
    def test_export_is_stable_and_lines_follow_signoff_order(self) -> None:
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        self.make_transfer("b", service_date="2026-09-11", departed="2026-09-11T08:00:00Z", signed="39900", signed_at="2026-09-11T12:00:00Z")
        first = self.service.export_quarter("audit", "2026Q3")
        second = self.service.export_quarter("audit", "2026Q3")
        self.assertEqual(first, second)
        self.assertEqual(len(first["content_sha256"]), 64)
        self.assertEqual([line["transfer_id"] for line in first["lines"]], ["tr-b", "tr-a"])
        self.assertEqual(first["state"], "open")
        self.set_clock("2026-10-01T08:00:00Z")
        self.service.seal_quarter("emis", "2026Q3")
        sealed_export = self.service.export_quarter("audit", "2026Q3")
        self.assertEqual(sealed_export["state"], "sealed")
        self.assertEqual(sealed_export["content_sha256"], first["content_sha256"])

    def test_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_emission_factor("dispatch", {"factor_id": "ef-x", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "10", "equipment_generation": "pre_retrofit", "effective_from": "2026-01-01T00:00:00Z"})
        with self.assertRaises(Forbidden):
            self.service.preview_quarter("dispatch", "2026Q3")
        with self.assertRaises(Forbidden):
            self.service.seal_quarter("audit", "2026Q3")
        with self.assertRaises(Forbidden):
            self.service.sign_transfer("emis", "tr-x", "1", "2026-09-01T00:00:00Z")
        self.register_factors()
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        self.service.seal_quarter("emis", "2026Q3")
        statement = self.service.quarter_statement("audit", "2026Q3")
        self.assertEqual(statement["state"], "sealed")
        self.assertEqual(self.service.export_quarter("audit", "2026Q3")["state"], "sealed")

    def test_api_exposes_emissions_flow(self) -> None:
        app = JsonApplication(self.service)
        officer = {"X-Actor-Id": "emis"}
        auditor = {"X-Actor-Id": "audit"}
        payload = {"route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "14.5", "equipment_generation": "pre_retrofit", "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-09-01T00:00:00Z"}
        response = app.handle("POST", "/emissions/factors", officer, json.dumps({**payload, "factor_id": "ef-pre"}).encode())
        self.assertEqual(response.status, 201)
        response = app.handle("POST", "/emissions/factors", officer, json.dumps({"factor_id": "ef-post", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "11.2", "equipment_generation": "post_retrofit", "effective_from": "2026-09-01T00:00:00Z"}).encode())
        self.assertEqual(response.status, 201)
        self.make_transfer("a", service_date="2026-09-10", departed="2026-09-10T08:00:00Z", signed="40000", signed_at="2026-09-12T08:00:00Z")
        response = app.handle("GET", "/emissions/quarters/2026Q3/preview", officer)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["totals"]["total_kgco2e"], "448000.000")
        response = app.handle("POST", "/emissions/quarters/2026Q3/seal", officer, b"")
        self.assertEqual(response.status, 200)
        response = app.handle("GET", "/emissions/quarters/2026Q3", auditor)
        self.assertEqual(response.body["state"], "sealed")
        response = app.handle("GET", "/emissions/quarters/2026Q3/export", auditor)
        self.assertEqual(len(response.body["content_sha256"]), 64)
        response = app.handle("GET", "/emissions/factors?route_id=pipe-a-b&product=crude", auditor)
        self.assertEqual(len(response.body["versions"]), 2)


class CliExportTests(unittest.TestCase):
    def test_cli_export_is_byte_stable_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "cli.sqlite3"
            connection = connect(database)
            clock = FrozenClock(datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc))
            service = SupplyService(connection, clock)
            service.create_user("plan", "plan", "planner")
            service.create_user("dispatch", "dispatch", "dispatcher")
            service.create_user("audit", "audit", "auditor")
            service.create_user("emis", "emis", "emissions_officer")
            service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
            service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
            service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
            service.register_emission_factor("emis", {"factor_id": "ef-post", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "11.2", "equipment_generation": "post_retrofit", "effective_from": "2026-01-01T00:00:00Z"})
            service.submit_nomination("dispatch", {"nomination_id": "nom-a", "route_id": "pipe-a-b", "shipper_id": "refinery", "service_date": "2026-09-10", "requested_barrels": "40000", "priority": 10, "idempotency_key": "key-a"})
            service.allocate("dispatch", "pipe-a-b", "2026-09-10")
            service.add_inventory_lot("dispatch", {"lot_id": "lot-a", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "60000", "unit_cost_usd": "90", "received_at": "2026-01-01T00:00:00Z"})
            clock.current = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
            service.dispatch_transfer("dispatch", "tr-a", "nom-a", "lot-a", 2)
            clock.current = datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc)
            service.sign_transfer("dispatch", "tr-a", "40000", "2026-09-12T08:00:00Z")
            service.seal_quarter("emis", "2026Q3")
            connection.close()
            outputs: list[str] = []
            for _ in range(2):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = emissions_cli.main(["--database", str(database), "--quarter", "2026Q3", "--actor-id", "audit"])
                outputs.append(buffer.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            document = json.loads(outputs[0])
            self.assertEqual(document["state"], "sealed")
            self.assertEqual(document["totals"]["total_kgco2e"], "448000.000")
            self.assertEqual(len(document["content_sha256"]), 64)
            error_buffer = io.StringIO()
            with contextlib.redirect_stderr(error_buffer):
                code = emissions_cli.main(["--database", str(database), "--quarter", "2026Q3", "--actor-id", "nobody"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(error_buffer.getvalue())["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
