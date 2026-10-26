# H2O queries — attribution and license

The queries in this directory are based on the groupby (`g1`–`g10`) and join
(`j1`–`j5`) queries of the H2O.ai
[db-benchmark](https://github.com/h2oai/db-benchmark), which is licensed under
the [Mozilla Public License 2.0 (MPL-2.0)](https://www.mozilla.org/MPL/2.0/).
The data is produced by that benchmark's data generator (`datagen.R`) and
rewritten into Parquet for this harness.

**Changes made.** The queries are written out as one SQL file each, in the form
Opteryx runs. The harness binds the bare table names (`x`, `small`, …) to the
corpus being read at run time.

**License of these files.** Unlike the rest of this repository (Apache 2.0),
the `.sql` files in this directory are derived from MPL-2.0 material and remain
under the MPL-2.0. Their source form is the files themselves, available in this
repository, and the original is at the link above. The MPL-2.0 is file-level:
it applies to these files and does not extend to the rest of this repository.

**Purpose.** These queries are used to track the Opteryx engine's performance
over time, to identify regressions, confirm improvements, and demonstrate that
the engine is not over-fitted to any one benchmark. See the repository README.
