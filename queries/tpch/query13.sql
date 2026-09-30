/*
DEVIATION FROM THE SPEC TEXT (TPC-H 2.4.13), forced by the engine.

The functional definition puts `o_comment not like '%[WORD1]%[WORD2]%'` in the
LEFT OUTER JOIN's ON clause. Opteryx only supports equality predicates in ON
("Only JOINs with equals comparisons supported"), and moving the predicate to
WHERE is NOT equivalent: it drops customers whose orders all fail the filter,
and customers with no orders, instead of counting them at c_count = 0.

So the orders are filtered in a derived table first. That reproduces the ON
semantics exactly and keeps the join an equality join. It is not Appendix B's
Variant A (which uses a view), so this is a non-conforming rewrite. Revisit when
the engine supports non-equality ON predicates.

WORD1 / WORD2: validation uses special / requests (2.4.13.4).
*/

SELECT
  c_count,
  Count(*) AS custdist
FROM
  (
    SELECT
      c_custkey,
      Count(o_orderkey) AS c_count
    FROM
      testdata.tpch.customer
      LEFT OUTER JOIN (
        SELECT
          *
        FROM
          testdata.tpch.orders
        WHERE
          o_comment NOT LIKE '%@WORD1@%@WORD2@%'
      ) AS t ON c_custkey = t.o_custkey
    GROUP BY
      c_custkey
  ) c_orders
GROUP BY
  c_count
ORDER BY
  custdist DESC,
  c_count DESC;
