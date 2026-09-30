/*
DEVIATION FROM THE SPEC TEXT (TPC-H 2.4.15), forced by the engine.

The spec creates a view and compares `total_revenue` to `(select max(...))`.
Appendix B Variant A is the same with a common table expression. Opteryx cannot
run Variant A: the scalar subquery over the CTE fails with "a probe-side join key
the engine could not resolve here is not supported". The maximum is therefore
computed in its own CTE and joined. Results are the same; the plan is not, so
this is a non-conforming rewrite. Revisit when the engine runs Variant A.
*/
with revenue_cached as
(select
    l_suppkey as supplier_no,
    sum(l_extendedprice * (1 - l_discount)) as total_revenue
from
    testdata.tpch.lineitem
where
    l_shipdate >= '@DATE@'::DATE
    and l_shipdate < '@DATE@'::DATE + interval '3' month
group by l_suppkey)

, max_revenue_cached as
(select
    max(total_revenue) as max_revenue
from
    revenue_cached)

select
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
from
    testdata.tpch.supplier,
    revenue_cached,
    max_revenue_cached
where
    s_suppkey = supplier_no
    and total_revenue = max_revenue
order by s_suppkey;
