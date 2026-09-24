"""季度排放核算明细的命令行导出，输出顺序与内容摘要保持稳定。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .emissions import EmissionsService
from .errors import SupplyError
from .planning import canonical_json
from .storage import connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出季度排放核算明细（顺序与内容摘要固定）")
    parser.add_argument("--database", type=Path, required=True, help="SQLite 数据库路径")
    parser.add_argument("--actor", required=True, help="具备排放读取权限的操作者编号")
    parser.add_argument("--quarter", required=True, help="形如 2026Q1 的季度")
    args = parser.parse_args(argv)
    connection = connect(args.database)
    try:
        export = EmissionsService(connection).export_quarter(args.actor, args.quarter)
    except SupplyError as exc:
        print(
            json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    finally:
        connection.close()
    print(canonical_json(export))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
