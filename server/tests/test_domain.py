from decimal import Decimal

import pytest

from app.domain.money import LineAmounts, calculate_line, sum_lines
from app.domain.numbers import exact_decimal
from app.domain.quantities import net_accepted, pending_problem, validate_receipt


def test_tax_inclusive_and_exclusive_follow_different_authoritative_bases():
    inclusive = calculate_line("1", "1.005", "13.0000", "tax_inclusive")
    assert inclusive == LineAmounts(Decimal("0.89"), Decimal("0.12"), Decimal("1.01"))
    exclusive = calculate_line("1", "1.005", "13.0000", "tax_exclusive")
    assert exclusive == LineAmounts(Decimal("1.01"), Decimal("0.13"), Decimal("1.14"))


def test_round_per_line_then_sum_and_keep_unit_price_precision():
    row = calculate_line("1", "0.005", "0", "tax_inclusive")
    assert row.gross == Decimal("0.01")
    assert sum_lines([row, row]).gross == Decimal("0.02")
    assert calculate_line("2", "0.005", "0", "tax_inclusive").gross == Decimal("0.01")
    assert calculate_line("3", "0.333333", "13", "tax_inclusive").gross == Decimal("1.00")


@pytest.mark.parametrize("basis", ["tax_inclusive", "tax_exclusive"])
def test_negative_rounding_is_symmetric_and_line_components_conserve_amount(basis):
    positive = calculate_line("1", "0.005", "13", basis)
    negative = calculate_line("-1", "0.005", "13", basis)
    assert negative.net == -positive.net
    assert negative.tax == -positive.tax
    assert negative.gross == -positive.gross
    assert negative.net + negative.tax == negative.gross


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "not-a-number", 0.1])
def test_nonfinite_or_inexact_input_is_rejected(value):
    with pytest.raises(ValueError):
        exact_decimal(value, 6)


def test_input_precision_is_checked_before_calculation():
    assert exact_decimal("0.000001", 6) == Decimal("0.000001")
    with pytest.raises(ValueError):
        calculate_line("0.0000001", "1", "13", "tax_inclusive")
    with pytest.raises(ValueError):
        calculate_line("1", "1", "13.00001", "tax_inclusive")


def test_receipt_dispositions_and_returns_do_not_double_deduct():
    validate_receipt("100", "90", "10")
    assert pending_problem("10", "0", "10") == 0
    # Returning problem goods is not an accepted-goods return.
    assert net_accepted("90", "0", "0") == 90
    assert net_accepted("90", "4", "0") == 94
    assert net_accepted("90", "0", "5") == 85
    with pytest.raises(ValueError):
        validate_receipt("100", "90", "9")
    with pytest.raises(ValueError):
        pending_problem("10", "4", "7")
    with pytest.raises(ValueError):
        net_accepted("90", "0", "91")
