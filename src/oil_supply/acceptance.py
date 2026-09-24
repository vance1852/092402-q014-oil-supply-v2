"""贯通报价、线路、库存、提名、情景分析和排放核算的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = SupplyService(connection, clock)
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor"), ("emis", "emissions_officer")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"price_index": "BRENT", "trade_date": f"2026-09-{index}", "close_usd": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部油田", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_barrels": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_barrels": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "BRENT", "quantity_barrels": "150000", "unit_cost_usd": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_barrels": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键管道恢复与需求回落", "price_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    service.register_emission_factor("emis", {"factor_id": "ef-crude-pre", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "14.5", "equipment_generation": "pre_retrofit", "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-09-01T00:00:00Z"})
    service.register_emission_factor("emis", {"factor_id": "ef-crude-post", "route_id": "pipe-a-b", "product": "crude", "kgco2e_per_barrel": "11.2", "equipment_generation": "post_retrofit", "effective_from": "2026-09-01T00:00:00Z"})
    clock.advance(hours=40)
    signoff = service.sign_transfer("dispatch", "transfer-001", "79750", "2026-09-26T00:00:00Z")
    service.preview_quarter("emis", "2026Q3")
    sealed = service.seal_quarter("emis", "2026Q3")
    first_export = service.export_quarter("audit", "2026Q3")
    second_export = service.export_quarter("audit", "2026Q3")
    emissions = {
        "quarter_id": sealed["quarter_id"],
        "signoff": signoff,
        "total_kgco2e": sealed["totals"]["total_kgco2e"],
        "disputed_loss_barrels": sealed["totals"]["disputed_loss_barrels"],
        "content_sha256": first_export["content_sha256"],
        "export_stable": first_export == second_export,
    }
    result = {"status": "ok", "price": service.price_summary("BRENT"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "emissions": emissions, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行油气供应服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
