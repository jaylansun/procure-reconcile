"""逐行金额计算的结果传输对象。"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LineAmountsDTO:
    """行金额计算结果的内部传输对象，携带未税额、税额和含税额。"""

    net: Decimal
    tax: Decimal
    gross: Decimal
