"""TPC-H substitution parameters (TPC-H v3.0.1, Clause 2.4).

The query files under queries/tpch/ are templates: every substitution parameter
is an `@NAME@` token, filled from one of two sets here.

    VALIDATION   the values Clause 2.4.x.4 fixes for validating against the
                 qualification database (SF1). harness/validate_tpch.py runs
                 these and compares against the published answers.
    BENCH        the values the daily suite has always run. Kept as they were
                 so the trend lines do not move; all are inside the ranges the
                 spec allows, but only some equal the validation values.

Stdlib-only: read on the control side as well as on the box.
"""

from __future__ import annotations

import re
from decimal import Decimal

VALIDATION: dict[int, dict[str, str]] = {
    1: {"DELTA": "90"},
    2: {"SIZE": "15", "TYPE": "BRASS", "REGION": "EUROPE"},
    3: {"SEGMENT": "BUILDING", "DATE": "1995-03-15"},
    4: {"DATE": "1993-07-01"},
    5: {"REGION": "ASIA", "DATE": "1994-01-01"},
    6: {"DATE": "1994-01-01", "DISCOUNT": "0.06", "QUANTITY": "24"},
    7: {"NATION1": "FRANCE", "NATION2": "GERMANY"},
    8: {"NATION": "BRAZIL", "REGION": "AMERICA", "TYPE": "ECONOMY ANODIZED STEEL"},
    9: {"COLOR": "green"},
    10: {"DATE": "1993-10-01"},
    11: {"NATION": "GERMANY"},  # FRACTION = 0.0001 / SF, filled in by fill()
    12: {"SHIPMODE1": "MAIL", "SHIPMODE2": "SHIP", "DATE": "1994-01-01"},
    13: {"WORD1": "special", "WORD2": "requests"},
    14: {"DATE": "1995-09-01"},
    15: {"DATE": "1996-01-01"},
    16: {
        "BRAND": "Brand#45", "TYPE": "MEDIUM POLISHED",
        "SIZE1": "49", "SIZE2": "14", "SIZE3": "23", "SIZE4": "45",
        "SIZE5": "19", "SIZE6": "3", "SIZE7": "36", "SIZE8": "9",
    },
    17: {"BRAND": "Brand#23", "CONTAINER": "MED BOX"},
    18: {"QUANTITY": "300"},
    19: {
        "BRAND1": "Brand#12", "BRAND2": "Brand#23", "BRAND3": "Brand#34",
        "QUANTITY1": "1", "QUANTITY2": "10", "QUANTITY3": "20",
    },
    20: {"COLOR": "forest", "DATE": "1994-01-01", "NATION": "CANADA"},
    21: {"NATION": "SAUDI ARABIA"},
    22: {"I1": "13", "I2": "31", "I3": "23", "I4": "29", "I5": "30", "I6": "18", "I7": "17"},
}

# The suite's fixed set. Where it differs from VALIDATION the spec's own
# "randomly selected" ranges apply; every value below is a legal draw.
BENCH: dict[int, dict[str, str]] = {
    **VALIDATION,
    1: {"DELTA": "76"},
    2: {"SIZE": "37", "TYPE": "COPPER", "REGION": "EUROPE"},
    3: {"SEGMENT": "BUILDING", "DATE": "1995-03-22"},
    4: {"DATE": "1996-05-01"},
    5: {"REGION": "AFRICA", "DATE": "1993-01-01"},
    6: {"DATE": "1993-01-01", "DISCOUNT": "0.06", "QUANTITY": "25"},
    7: {"NATION1": "KENYA", "NATION2": "PERU"},
    8: {"NATION": "PERU", "REGION": "AMERICA", "TYPE": "ECONOMY BURNISHED NICKEL"},
    9: {"COLOR": "plum"},
    10: {"DATE": "1993-07-01"},
    12: {"SHIPMODE1": "REG AIR", "SHIPMODE2": "MAIL", "DATE": "1995-01-01"},
    14: {"DATE": "1995-08-01"},
    16: {
        "BRAND": "Brand#34", "TYPE": "ECONOMY BRUSHED",
        "SIZE1": "22", "SIZE2": "14", "SIZE3": "27", "SIZE4": "49",
        "SIZE5": "21", "SIZE6": "33", "SIZE7": "35", "SIZE8": "28",
    },
    19: {
        "BRAND1": "Brand#32", "BRAND2": "Brand#35", "BRAND3": "Brand#24",
        "QUANTITY1": "7", "QUANTITY2": "15", "QUANTITY3": "26",
    },
}

PARAM_SETS = {"validation": VALIDATION, "bench": BENCH}

_TOKEN = re.compile(r"@([A-Z0-9_]+)@")


def fill(sql: str, number: int, params: str, scale_factor: str) -> str:
    """Replace every @NAME@ in one query's text; any token left over is an error.

    Q11's FRACTION is 0.0001 / SF, derived here as a Decimal so 10 gives
    0.00001 exactly.
    """
    values = dict(PARAM_SETS[params][number])
    if number == 11:
        values["FRACTION"] = format(Decimal("0.0001") / Decimal(scale_factor), "f")
    text = _TOKEN.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), sql)
    left = _TOKEN.findall(text)
    if left:
        raise ValueError(f"q{number:02d}: unfilled parameters {sorted(set(left))}")
    return text
