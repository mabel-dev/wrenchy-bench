/*
TPC-H 2.4.11 functional query definition. FRACTION is 0.0001 / SF, filled in
by harness/tpch_params.py from the line's scale factor.
*/
select
    ps_partkey,
    sum(ps_supplycost * ps_availqty) as value
from
    testdata.tpch.partsupp,
    testdata.tpch.supplier,
    testdata.tpch.nation
where
    ps_suppkey = s_suppkey
    and s_nationkey = n_nationkey
    and n_name = '@NATION@'
group by
    ps_partkey having
        sum(ps_supplycost * ps_availqty) > (
            select
                sum(ps_supplycost * ps_availqty) * @FRACTION@
            from
                testdata.tpch.partsupp,
                testdata.tpch.supplier,
                testdata.tpch.nation
            where
                ps_suppkey = s_suppkey
                and s_nationkey = n_nationkey
                and n_name = '@NATION@'
        )
order by
    value desc;
