# Join Order Benchmark (JOB) queries — attribution

The 113 queries in this directory are the Join Order Benchmark queries from:

> Viktor Leis, Andrey Gubichev, Atanas Mirchev, Peter Boncz, Alfons Kemper,
> Thomas Neumann. "How Good Are Query Optimizers, Really?"
> *Proceedings of the VLDB Endowment* 9(3), 2015.

They are used as published in
[gregrahn/join-order-benchmark](https://github.com/gregrahn/join-order-benchmark).
That repository states no licence, so these files are credited to their
authors and are not covered by this repository's Apache 2.0 licence. The
harness only qualifies table names with the corpus name at run time.

**Data.** The benchmark data is derived from the IMDb dataset (the May 2013
snapshot distributed by CWI), rewritten into Parquet for this harness. It is
used for non-commercial performance tracking only and is not published or
redistributed; the corpora are held in private storage.

> Information courtesy of IMDb (https://www.imdb.com). Used with permission.

IMDb's [conditions](https://help.imdb.com/article/imdb/general-information/can-i-use-imdb-data-in-my-software/G5JTRESSHJBBHTGX)
for its datasets apply to that data.

**Purpose.** These queries are used to track the Opteryx engine's performance
over time, to identify regressions, confirm improvements, and demonstrate that
the engine is not over-fitted to any one benchmark. See the repository README.
