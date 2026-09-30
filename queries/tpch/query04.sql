select
    o_orderpriority,
    count(*) as order_count
from
    testdata.tpch.orders as o
where
    o_orderdate >= '@DATE@'::DATE
    and o_orderdate < '@DATE@'::DATE + interval '3' month
    and exists (
        select
            *
        from
            testdata.tpch.lineitem
        where
            l_orderkey = o.o_orderkey
            and l_commitdate < l_receiptdate
    )
group by
    o_orderpriority
order by
    o_orderpriority;
