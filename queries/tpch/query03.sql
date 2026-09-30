select
    l_orderkey,
    sum(l_extendedprice * (1 - l_discount)) as revenue,
    o_orderdate,
    o_shippriority
from
    testdata.tpch.customer,
    testdata.tpch.orders,
    testdata.tpch.lineitem
where
    c_mktsegment = '@SEGMENT@'
    and c_custkey = o_custkey
    and l_orderkey = o_orderkey
    and o_orderdate < '@DATE@'::DATE
    and l_shipdate > '@DATE@'::DATE
group by
    l_orderkey,
    o_orderdate,
    o_shippriority
order by
    revenue desc,
    o_orderdate
limit 10;
