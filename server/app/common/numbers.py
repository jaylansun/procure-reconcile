from decimal import Decimal, InvalidOperation


def exact_decimal(value: str | Decimal, max_places: int) -> Decimal:
    """Reject floats, nonfinite values and extra input fractional digits.

    The digit budget is an explicit computation-resource limit, not a chosen
    PostgreSQL column precision or a business amount limit.
    """
    if not isinstance(value, (str, Decimal)):
        raise ValueError("精确数值须使用十进制字符串或Decimal，不能使用浮点数")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("无效十进制数值") from error
    if not result.is_finite():
        raise ValueError("数值必须为有限值")
    if result.as_tuple().exponent < -max_places:
        raise ValueError(f"数值最多允许{max_places}位小数")
    if len(result.as_tuple().digits) > 1000 or result.adjusted() > 1000:
        raise ValueError("数值超出计算资源上限")
    return result
