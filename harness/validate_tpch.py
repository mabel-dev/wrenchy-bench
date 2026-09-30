"""TPC-H query validation at SF1 (TPC-H v3.0.1, Clause 2.3).

Runs all 22 queries with the spec's validation parameters against the SF1
corpus and compares each result to the published answer. Correctness, not
speed: nothing here is timed or written to a bundle.

    cd <dir containing testdata/>
    python harness/validate_tpch.py [--relation testdata.tpch_1_skene] [--only 6,13]

The answers come from DuckDB's `tpch_answers()` (the TPC answer set for SF1), so
`pip install duckdb` is needed and its tpch extension must be installable once.
Appendix C itself is only distributed to TPC members.

Verdicts
    PASS   every row and column matches
    FAIL   a difference the corpus cannot explain: a wrong answer
    DATA   differs, but the query reads a free-text column that this corpus does
           not carry as dbgen wrote it (see DATA_DEPENDENT); not an engine bug
    ERROR  the query did not run

Comparison is stricter than Clause 2.1.3.5: numbers must agree to the cent (the
answers are printed to 2 decimals), not within $100 or 1%.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from harness import bench_runner  # noqa: E402
from harness.config import SUITE_BY_ID  # noqa: E402

# Free-text columns (comments, addresses) differ between the SF1 corpus and
# DuckDB's dbgen output on every row; every numeric, date, key and enum column
# is identical. Found by diffing testdata/tpch_1 against `CALL dbgen(sf=1)` on
# 2026-09-28. Queries that only PRINT such a column are compared with it masked;
# a query that FILTERS on one can miss the published answer (Q13 does; Q16's
# s_comment filter happens to select the same suppliers, so it is compared in full).
MASKED_COLUMNS = {
    2: {"s_address", "s_comment"},
    10: {"c_address", "c_comment"},
    15: {"s_address"},
    20: {"s_address"},
}
DATA_DEPENDENT = {
    13: "filters o_comment, which differs from dbgen's",
}

CENT = Decimal("0.006")  # answers are printed to 2 decimals: half a cent, plus slack


def expected_answers() -> dict[int, tuple[list[str], list[list[str]]]]:
    try:
        import duckdb
    except ImportError:
        raise SystemExit("validate_tpch needs duckdb for the TPC answer set: pip install duckdb")
    connection = duckdb.connect()
    connection.execute("INSTALL tpch")
    connection.execute("LOAD tpch")
    answers = {}
    for number, text in connection.execute(
        "select query_nr, answer from tpch_answers() where scale_factor = 1"
    ).fetchall():
        lines = [line for line in text.splitlines() if line.strip()]
        header = [name.strip().lower() for name in lines[0].split("|")]
        answers[int(number)] = (header, [line.split("|") for line in lines[1:]])
    return answers


def normal(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def same_cell(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    try:
        return abs(Decimal(actual) - Decimal(expected)) <= CENT
    except InvalidOperation:
        return False


def compare(number: int, columns: list[str], rows: list[list], header: list[str], expected: list[list[str]]):
    """(verdict, detail). Column NAMES are not compared, only their order and count."""
    if len(columns) != len(header):
        return "FAIL", f"{len(columns)} columns, expected {len(header)}"
    masked = {i for i, name in enumerate(header) if name in MASKED_COLUMNS.get(number, ())}
    if len(rows) != len(expected):
        return "FAIL", f"{len(rows)} rows, expected {len(expected)}"
    for index, (got, want) in enumerate(zip(rows, expected)):
        for column, (a, b) in enumerate(zip(got, want)):
            if column in masked:
                continue
            if not same_cell(normal(a), b.strip()):
                return "FAIL", f"row {index + 1}, {header[column]}: got {normal(a)!r}, expected {b.strip()!r}"
    return "PASS", ""


def run_query(opteryx, sql: str) -> tuple[list[str], list[list]]:
    rows: list[list] = []
    columns: list[str] = []
    for morsel in opteryx.session().execute_to_morsels(sql):
        if morsel is None:
            continue
        columns = list(morsel.column_names)
        rows.extend(list(row) for row in morsel)
    return columns, rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the TPC-H queries at SF1")
    parser.add_argument("--relation", default=SUITE_BY_ID["tpch_sf1_skene"].relation,
                        help="dataset holding the SF1 tables (default: the suite's SF1 line)")
    parser.add_argument("--only", default=None, help="comma-separated query numbers, e.g. 6,13")
    parser.add_argument("--data-root", default=None, help="directory containing testdata/ (chdir here)")
    args = parser.parse_args()

    if args.data_root:
        os.chdir(args.data_root)
    only = {int(n) for n in args.only.split(",")} if args.only else None

    line = dataclasses.replace(SUITE_BY_ID["tpch_sf1_skene"], relation=args.relation)
    opteryx = bench_runner.bind_engine()
    answers = expected_answers()

    counts = {"PASS": 0, "FAIL": 0, "DATA": 0, "ERROR": 0}
    print(f"\nTPC-H validation · SF1 · {args.relation} · validation parameters\n")
    for name, sql in bench_runner.load_queries(line, params="validation"):
        number = int(name[1:])
        if only and number not in only:
            continue
        header, expected = answers[number]
        started = time.perf_counter()
        try:
            columns, rows = run_query(opteryx, sql)
            verdict, detail = compare(number, columns, rows, header, expected)
        except Exception as exception:  # noqa: BLE001 — a failing query is a result
            verdict, detail = "ERROR", f"{type(exception).__name__}: {exception}"[:200]
        if verdict == "FAIL" and number in DATA_DEPENDENT:
            verdict, detail = "DATA", f"{DATA_DEPENDENT[number]} ({detail})"
        counts[verdict] += 1
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  {name}  {verdict:<5} {elapsed:8.0f} ms  {detail}")

    print("\n  " + "   ".join(f"{k} {v}" for k, v in counts.items()))
    return 1 if counts["FAIL"] or counts["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
