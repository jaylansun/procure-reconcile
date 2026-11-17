"""按已确认价格口径计算金额并逐行舍入汇总。"""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

from app.common.numbers import exact_decimal
from app.domain.dto.line_amounts_dto import LineAmountsDTO

CALCULATION_VERSION = "line-half-up-v1"
CENT = Decimal("0.01")


class MoneyService:
    """提供含税、未税行金额计算和单据金额汇总。"""

    @staticmethod
    def calculate_line(
        quantity: str | Decimal,
        unit_price: str | Decimal,
        tax_percent: str | Decimal,
        basis: Literal["tax_inclusive", "tax_exclusive"],
    ) -> LineAmountsDTO:
        """根据输入价格口径计算一行未税额、税额和含税额。"""
        q = exact_decimal(quantity, 6)
        p = exact_decimal(unit_price, 6)
        percentage = exact_decimal(tax_percent, 4)
        if percentage < 0:
            raise ValueError("税率不能为负")
        if basis not in {"tax_inclusive", "tax_exclusive"}:
            raise ValueError("价格口径无效")
        with localcontext() as context:
            # 先为乘除法保留足够精度，再明确舍入到分。
            context.prec = max(50, q.adjusted() + p.adjusted() + percentage.adjusted() + 30)
            rate = percentage / Decimal(100)

            def rounded(value: Decimal) -> Decimal:
                result = value.quantize(CENT, rounding=ROUND_HALF_UP)
                return result if result else Decimal("0.00")

            if basis == "tax_inclusive":
                gross = rounded(q * p)
                net = rounded(gross / (1 + rate))
                tax = gross - net
            else:
                net = rounded(q * p)
                tax = rounded(net * rate)
                gross = net + tax
            return LineAmountsDTO(net=net, tax=tax, gross=gross)

    @staticmethod
    def sum_lines(lines: Iterable[LineAmountsDTO]) -> LineAmountsDTO:
        """汇总各行已舍入的金额，保留精确十进制结果。"""
        rows = list(lines)
        with localcontext() as context:
            context.prec = max(
                50,
                max(
                    (value.adjusted() for row in rows for value in (row.net, row.tax, row.gross)),
                    default=0,
                )
                + len(str(len(rows)))
                + 10,
            )
            return LineAmountsDTO(
                net=sum((row.net for row in rows), Decimal("0.00")),
                tax=sum((row.tax for row in rows), Decimal("0.00")),
                gross=sum((row.gross for row in rows), Decimal("0.00")),
            )
