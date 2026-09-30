select
    p_brand,
    p_type,
    p_size,
    count(distinct ps_suppkey) as supplier_cnt
from
    testdata.tpch.partsupp,
    testdata.tpch.part
where
    p_partkey = ps_partkey
    and p_brand <> '@BRAND@'
    and p_type not like '@TYPE@%'
    and p_size in (@SIZE1@, @SIZE2@, @SIZE3@, @SIZE4@, @SIZE5@, @SIZE6@, @SIZE7@, @SIZE8@)
    and partsupp.ps_suppkey not in (
        select
            s_suppkey
        from
            testdata.tpch.supplier
        where
            s_comment like '%Customer%Complaints%'
    )
group by
    p_brand,
    p_type,
    p_size
order by
    supplier_cnt desc,
    p_brand,
    p_type,
    p_size;
