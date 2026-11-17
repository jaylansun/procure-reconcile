"""收货与问题处置数量校验及计算。"""

from decimal import Decimal, localcontext

from app.common.numbers import exact_decimal


class QuantityService:
    """按已确认精度计算收货、问题货及净接受数量。"""

    @staticmethod
    def quantities(*values: str | Decimal) -> list[Decimal]:
        """校验数量精度及非负约束，转换为 Decimal。"""
        parsed = [exact_decimal(value, 6) for value in values]
        if any(value < 0 for value in parsed):
            raise ValueError("数量不能为负")
        return parsed

    @staticmethod
    def validate_receipt(
        arrived: str | Decimal, accepted: str | Decimal, problem: str | Decimal
    ) -> None:
        """校验真实到货量与初始接受、问题数量守恒。"""
        actual, good, pending = QuantityService.quantities(arrived, accepted, problem)
        with localcontext() as context:
            context.prec = max(50, max(actual.adjusted(), good.adjusted(), pending.adjusted()) + 15)
            if actual != good + pending:
                raise ValueError("到货量必须等于初始接受量加初始问题/待处理量")

    @staticmethod
    def pending_problem(
        initial: str | Decimal, later_accepted: str | Decimal, returned_problem: str | Decimal
    ) -> Decimal:
        """计算问题来源经过转接受和退回后的剩余量。"""
        original, accepted, returned = QuantityService.quantities(
            initial, later_accepted, returned_problem
        )
        with localcontext() as context:
            context.prec = max(50, max(v.adjusted() for v in (original, accepted, returned)) + 15)
            remaining = original - accepted - returned
            if remaining < 0:
                raise ValueError("处置数量不能超出问题来源剩余量")
            return remaining

    @staticmethod
    def net_accepted(
        initial: str | Decimal, later_accepted: str | Decimal, returned_accepted: str | Decimal
    ) -> Decimal:
        """计算接受来源经过转接受和退回后的净接受量。"""
        original, accepted, returned = QuantityService.quantities(
            initial, later_accepted, returned_accepted
        )
        with localcontext() as context:
            context.prec = max(50, max(v.adjusted() for v in (original, accepted, returned)) + 15)
            remaining = original + accepted - returned
            if remaining < 0:
                raise ValueError("已接受货退回量不能超出对应接受来源")
            return remaining
