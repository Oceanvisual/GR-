"""Numbers on the screens: rounding at display only, Russian notation (R-13, T046)."""

from decimal import Decimal

import pytest

from gr.web.format import NBSP, money, number, percent, plain


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("1234567.5", f"1{NBSP}234{NBSP}568{NBSP}₽"),  # half to even: 7.5 → 8
        ("1234566.5", f"1{NBSP}234{NBSP}566{NBSP}₽"),  # 6.5 → 6
        ("-1234.4", f"−1{NBSP}234{NBSP}₽"),
        ("-0.4", f"0{NBSP}₽"),  # never «−0»
    ],
)
def test_money_in_whole_roubles(value: str, shown: str) -> None:
    assert money(Decimal(value)) == shown


def test_number_percent_and_missing_values() -> None:
    assert number(Decimal("1234.25")) == f"1{NBSP}234,2"
    assert number(Decimal("0.55837563"), 2) == "0,56"
    assert percent(Decimal("0.4754098")) == f"48{NBSP}%"
    assert percent(Decimal("18.5")) == f"1{NBSP}850{NBSP}%"
    assert percent(None) == "не определён"
    assert money(None) == number(None) == "—"


def test_plain_is_exact_for_input_fields() -> None:
    assert plain(Decimal("1200")) == "1200"
    assert plain(Decimal("0.200")) == "0,2"
    assert plain(Decimal("0.1875")) == "0,1875"
    assert plain(Decimal("8400000.00")) == "8400000"
