   -- Pass 1/2 regression example: CASE scopes with AND, nested function calls, OVER (...),
   -- a joined subquery, a bare SELECT and a bare WHERE.
   SELECT ORDER_ROW.OrderId
        , CASE
          WHEN ORDER_ROW.ChannelCode = 'WEB'
           AND ORDER_ROW.ShippedDate IS NOT NULL
          THEN 1
          ELSE 0
           END                AS ShippedOnline
        , DATEADD(
                    DAY
                  , 30 * ORDER_ROW.ReturnWindowMonths + 7
                  , ORDER_ROW.ShippedDate
                 )            AS ReturnDeadline
          -- comments inside a scope sit at its argument column
        , ROW_NUMBER() OVER (
                             PARTITION BY ORDER_ROW.CustomerId
                                 ORDER BY ORDER_ROW.OrderDate
                                        , ORDER_ROW.OrderId
                            ) AS CustomerOrderRank
     FROM #order_build        AS ORDER_ROW
LEFT JOIN (
             SELECT LINE_COUNTS.OrderId
                  , COUNT(*)     AS OrderLines_N
               FROM #order_lines AS LINE_COUNTS
           GROUP BY LINE_COUNTS.OrderId
          )                   AS LINE_TOTALS
       ON LINE_TOTALS.OrderId = ORDER_ROW.OrderId
    WHERE ORDER_ROW.IsOpen = 1
          -- open orders only
      AND (
              ORDER_ROW.Email IS NOT NULL
           OR ORDER_ROW.Phone IS NOT NULL
          )
 ORDER BY CASE
          WHEN ORDER_ROW.OrderStatus = 'Backorder'
          THEN 1
          ELSE 2
           END
        , ORDER_ROW.OrderId
        ;
