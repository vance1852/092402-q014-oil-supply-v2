"""季度运输排放明细的命令行导出，明细顺序和内容摘要保持稳定。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import SupplyError
from .service import SupplyService
from .storage import connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出季度运输排放明细（顺序与内容摘要保持稳定）")
    parser.add_argument("--database", type=Path, default=Path("oil_supply.sqlite3"))
    parser.add_argument("--quarter", required=True, help="季度编号，例如 2026Q3")
    parser.add_argument("--actor-id", default="audit", help="具备 emission.read 权限的操作者编号")
    args = parser.parse_args(argv)
    connection = connect(args.database)
    try:
        service = SupplyService(connection)
        document = service.export_quarter(args.actor_id, args.quarter)
    except SupplyError as exc:
        print(
            json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    finally:
        connection.close()
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
