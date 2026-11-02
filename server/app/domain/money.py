from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

from app.domain.numbers import exact_decimal

CALCULATION_VERSION = "line-half-up-v1"
CENT = Decimal("0.01")


@dataclass(frozen=True)
class LineAmounts:
    net: Decimal
    tax: Decimal
    gross: Decimal


def calculate_line(
    quantity: str | Decimal,
    unit_price: str | Decimal,
    tax_percent: str | Decimal,
    basis: Literal["tax_inclusive", "tax_exclusive"],
) -> LineAmounts:
    q = exact_decimal(quantity, 6)
    p = exact_decimal(unit_price, 6)
    percentage = exact_decimal(tax_percent, 4)
    if percentage < 0:
        raise ValueError("税率不能为负")
    if basis not in {"tax_inclusive", "tax_exclusive"}:
        raise ValueError("价格口径无效")
    with localcontext() as context:
        # Enough guard digits for multiplication/division before explicit cent rounding.
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
        return LineAmounts(net=net, tax=tax, gross=gross)


def sum_lines(lines: Iterable[LineAmounts]) -> LineAmounts:
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
        return LineAmounts(
            net=sum((row.net for row in rows), Decimal("0.00")),
            tax=sum((row.tax for row in rows), Decimal("0.00")),
            gross=sum((row.gross for row in rows), Decimal("0.00")),
        )
