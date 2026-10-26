# ClickBench queries — attribution and license

The 43 queries in this directory are adapted from
[ClickBench](https://github.com/ClickHouse/ClickBench) by ClickHouse, Inc.,
which is licensed under the
[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
license (CC BY-NC-SA 4.0)](https://creativecommons.org/licenses/by-nc-sa/4.0/).

**Changes made.** The table name `hits` is replaced with the `{DATASET}`
placeholder, which the harness fills in per corpus, and some expressions are
rewritten for Opteryx's SQL dialect (for example explicit
`::TIMESTAMP[s]` / `::DATE` casts, and repeated expressions in place of select
aliases in `GROUP BY`). The queries are otherwise ClickBench's.

**Purpose.** These queries are used to track the Opteryx engine's performance
over time, to identify regressions, confirm improvements, and demonstrate that
the engine is not over-fitted to any one benchmark. See the repository README.

**License of these files.** Unlike the rest of this repository (Apache 2.0),
the `.sql` files in this directory are licensed under CC BY-NC-SA 4.0:
attribution, non-commercial use, and ShareAlike apply to them and to anything
adapted from them.
