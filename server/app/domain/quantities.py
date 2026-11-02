from decimal import Decimal, localcontext

from app.domain.numbers import exact_decimal


def quantities(*values: str | Decimal) -> list[Decimal]:
    parsed = [exact_decimal(value, 6) for value in values]
    if any(value < 0 for value in parsed):
        raise ValueError("数量不能为负")
    return parsed


def validate_receipt(
    arrived: str | Decimal, accepted: str | Decimal, problem: str | Decimal
) -> None:
    actual, good, pending = quantities(arrived, accepted, problem)
    with localcontext() as context:
        context.prec = max(50, max(actual.adjusted(), good.adjusted(), pending.adjusted()) + 15)
        if actual != good + pending:
            raise ValueError("到货量必须等于初始接受量加初始问题/待处理量")


def pending_problem(
    initial: str | Decimal, later_accepted: str | Decimal, returned_problem: str | Decimal
) -> Decimal:
    original, accepted, returned = quantities(initial, later_accepted, returned_problem)
    with localcontext() as context:
        context.prec = max(50, max(v.adjusted() for v in (original, accepted, returned)) + 15)
        remaining = original - accepted - returned
        if remaining < 0:
            raise ValueError("处置数量不能超出问题来源剩余量")
        return remaining


def net_accepted(
    initial: str | Decimal, later_accepted: str | Decimal, returned_accepted: str | Decimal
) -> Decimal:
    original, accepted, returned = quantities(initial, later_accepted, returned_accepted)
    with localcontext() as context:
        context.prec = max(50, max(v.adjusted() for v in (original, accepted, returned)) + 15)
        remaining = original + accepted - returned
        if remaining < 0:
            raise ValueError("已接受货退回量不能超出对应接受来源")
        return remaining
