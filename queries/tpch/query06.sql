select
    sum(l_extendedprice * l_discount) as revenue
from
    testdata.tpch.lineitem
where
    l_shipdate >= '@DATE@'::DATE
    and l_shipdate < '@DATE@'::DATE + interval '1' year
    and l_discount between @DISCOUNT@ - 0.01 and @DISCOUNT@ + 0.01
    and l_quantity < @QUANTITY@;
