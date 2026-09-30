select
    s_name,
    s_address
from
    testdata.tpch.supplier,
    testdata.tpch.nation
where
    s_suppkey in (
        select
            ps_suppkey
        from
            testdata.tpch.partsupp
        where
            ps_partkey in (
                select
                    p_partkey
                from
                    testdata.tpch.part
                where
                    p_name like '@COLOR@%'
            )
            and ps_availqty > (
                select
                    0.5 * sum(l_quantity)
                from
                    testdata.tpch.lineitem
                where
                    l_partkey = ps_partkey
                    and l_suppkey = ps_suppkey
                    and l_shipdate >= '@DATE@'::DATE
                    and l_shipdate < '@DATE@'::DATE + interval '1' year
            )
    )
    and s_nationkey = n_nationkey
    and n_name = '@NATION@'
order by
    s_name;
