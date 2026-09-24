"""运输排放核算的确定性计算、季度归属和稳定导出规则。

核算口径：
- 只认最终签收数量，在途转运不计入排放；
- 能耗因子按（线路、油品、生效时间）取半开区间 [effective_from, effective_to) 内唯一版本；
- 转运适用的因子以发运时间为准，用于区分设备改造前后的能耗水平；
- 转运按签收时间归属季度，跨期在途在期末明确列为排除项；
- 标准损耗为装船量与预计交付量之差，争议损耗为预计交付量与最终签收量之差。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .clock import parse_utc, utc_text
from .planning import decimal_text, digest


ZERO = Decimal("0")
QUARTER_ID = re.compile(r"^(\d{4})Q([1-4])$")
EQUIPMENT_GENERATIONS = ("pre_retrofit", "post_retrofit")
OPEN_ENDED = datetime.max.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class PrecisionRules:
    """封存进季度报表的精度规则，封存后不再随系统默认值变化。"""

    volume_places: int = 3
    emission_places: int = 3
    rounding: str = ROUND_HALF_UP

    def _quantum(self, places: int) -> Decimal:
        return Decimal(1).scaleb(-places)

    def volume(self, value: Decimal) -> Decimal:
        return value.quantize(self._quantum(self.volume_places), rounding=self.rounding)

    def emission(self, value: Decimal) -> Decimal:
        return value.quantize(self._quantum(self.emission_places), rounding=self.rounding)

    def as_dict(self) -> dict[str, object]:
        return {
            "volume_places": self.volume_places,
            "emission_places": self.emission_places,
            "rounding": self.rounding,
        }


DEFAULT_PRECISION = PrecisionRules()


def parse_quarter(quarter_id: str) -> tuple[int, int]:
    match = QUARTER_ID.fullmatch(quarter_id or "")
    if match is None:
        raise ValueError("季度编号必须是 YYYYQn 形式，例如 2026Q3")
    return int(match.group(1)), int(match.group(2))


def quarter_bounds(quarter_id: str) -> tuple[datetime, datetime]:
    """返回季度的半开区间 [起始, 下一季起始)。"""
    year, quarter = parse_quarter(quarter_id)
    start_month = (quarter - 1) * 3 + 1
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    if quarter == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, start_month + 3, 1, tzinfo=timezone.utc)
    return start, end


def quarter_of(moment: datetime) -> str:
    quarter = (moment.month - 1) // 3 + 1
    return f"{moment.year:04d}Q{quarter}"


def next_quarter(quarter_id: str) -> str:
    year, quarter = parse_quarter(quarter_id)
    if quarter == 4:
        return f"{year + 1:04d}Q1"
    return f"{year:04d}Q{quarter + 1}"


def _end_or_open(effective_to: datetime | None) -> datetime:
    return OPEN_ENDED if effective_to is None else effective_to


def ranges_overlap(
    a_from: datetime,
    a_to: datetime | None,
    b_from: datetime,
    b_to: datetime | None,
) -> bool:
    return a_from < _end_or_open(b_to) and b_from < _end_or_open(a_to)


def resolve_factor(
    versions: Sequence[Mapping[str, object]],
    moment: datetime,
) -> Mapping[str, object] | None:
    """返回覆盖 moment 的唯一因子版本；没有覆盖时返回 None。"""
    covering = [
        version
        for version in versions
        if parse_utc(str(version["effective_from"]), "effective_from")
        <= moment
        < _end_or_open(
            None
            if version["effective_to"] in (None, "")
            else parse_utc(str(version["effective_to"]), "effective_to")
        )
    ]
    if not covering:
        return None
    return max(
        covering,
        key=lambda version: parse_utc(str(version["effective_from"]), "effective_from"),
    )


def transfer_emission(
    *,
    loaded: Decimal,
    expected_delivered: Decimal,
    signed: Decimal,
    kgco2e_per_barrel: Decimal,
    precision: PrecisionRules = DEFAULT_PRECISION,
) -> dict[str, str]:
    """单笔已签收转运的数量与排放结果，排放只按最终签收数量计算。"""
    standard_loss = precision.volume(loaded - expected_delivered)
    disputed_loss = precision.volume(expected_delivered - signed)
    kgco2e = precision.emission(signed * kgco2e_per_barrel)
    return {
        "signed_barrels": decimal_text(precision.volume(signed)),
        "standard_loss_barrels": decimal_text(standard_loss),
        "disputed_loss_barrels": decimal_text(disputed_loss),
        "kgco2e": decimal_text(kgco2e),
    }


def _factor_snapshot(lines: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    seen: dict[str, Mapping[str, object]] = {}
    for line in lines:
        factor = line["factor"]
        seen[str(factor["factor_id"])] = factor
    return [
        seen[key]
        for key in sorted(
            seen,
            key=lambda item: (
                str(seen[item]["route_id"]),
                str(seen[item]["effective_from"]),
                item,
            ),
        )
    ]


def summarize(
    lines: Sequence[Mapping[str, str]],
    adjustments: Sequence[Mapping[str, str]],
    precision: PrecisionRules = DEFAULT_PRECISION,
) -> dict[str, object]:
    signed = sum((Decimal(line["signed_barrels"]) for line in lines), ZERO)
    standard = sum((Decimal(line["standard_loss_barrels"]) for line in lines), ZERO)
    disputed = sum((Decimal(line["disputed_loss_barrels"]) for line in lines), ZERO)
    line_emissions = sum((Decimal(line["kgco2e"]) for line in lines), ZERO)
    adjustment_emissions = sum((Decimal(item["delta_kgco2e"]) for item in adjustments), ZERO)
    return {
        "transfer_count": len(lines),
        "signed_barrels": decimal_text(precision.volume(signed)),
        "standard_loss_barrels": decimal_text(precision.volume(standard)),
        "disputed_loss_barrels": decimal_text(precision.volume(disputed)),
        "lines_kgco2e": decimal_text(precision.emission(line_emissions)),
        "adjustment_count": len(adjustments),
        "adjustments_kgco2e": decimal_text(precision.emission(adjustment_emissions)),
        "total_kgco2e": decimal_text(precision.emission(line_emissions + adjustment_emissions)),
    }


def build_statement(
    *,
    quarter_id: str,
    lines: Sequence[Mapping[str, object]],
    adjustments: Sequence[Mapping[str, object]],
    excluded_in_transit: Sequence[Mapping[str, object]],
    precision: PrecisionRules = DEFAULT_PRECISION,
) -> dict[str, object]:
    """汇总季度报表；明细顺序固定，内容摘要对同一输入保持稳定。"""
    start, end = quarter_bounds(quarter_id)
    ordered_lines = sorted(lines, key=lambda item: (str(item["signed_at"]), str(item["transfer_id"])))
    ordered_adjustments = sorted(adjustments, key=lambda item: str(item["adjustment_id"]))
    ordered_excluded = sorted(
        excluded_in_transit,
        key=lambda item: (str(item["departed_at"]), str(item["transfer_id"])),
    )
    statement: dict[str, object] = {
        "quarter_id": quarter_id,
        "starts_at": utc_text(start),
        "ends_before": utc_text(end),
        "precision": precision.as_dict(),
        "factor_snapshot": _factor_snapshot(ordered_lines),
        "lines": ordered_lines,
        "adjustments": ordered_adjustments,
        "excluded_in_transit": ordered_excluded,
        "totals": summarize(ordered_lines, ordered_adjustments, precision),
    }
    statement["statement_sha256"] = digest(statement)
    return statement


def export_document(statement: Mapping[str, object], *, state: str) -> dict[str, object]:
    """命令行导出文档；内容摘要只覆盖明细、调整单和合计。"""
    content = {
        "quarter_id": statement["quarter_id"],
        "lines": statement["lines"],
        "adjustments": statement["adjustments"],
        "totals": statement["totals"],
    }
    return {
        "quarter_id": statement["quarter_id"],
        "state": state,
        "precision": statement["precision"],
        "factor_snapshot": statement["factor_snapshot"],
        "totals": statement["totals"],
        "lines": statement["lines"],
        "adjustments": statement["adjustments"],
        "content_sha256": digest(content),
    }
