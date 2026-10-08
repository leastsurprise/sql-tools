-- Revenue per active customer since a start date, in house style: one #temp per
-- population, a #params row, every temp table dropped after its last use.
   SELECT CAST(1 AS int)             AS idx
        , CAST('2026-07-01' AS date) AS StartDate
     INTO #params
        ;

   SELECT ORD.OrderId
        , ORD.CustomerId
        , ORD.OrderDate
     INTO #recent_orders
     FROM dbo.orders AS ORD
     JOIN #params    AS PARAMS
       ON PARAMS.idx = 1
    WHERE ORD.OrderDate >= PARAMS.StartDate
      AND ORD.OrderStatus <> 'Cancelled'
        ;

DROP TABLE #params;

   SELECT RECENT.CustomerId
        , SUM(LINE.Quantity * LINE.UnitPrice * (1 - ISNULL(LINE.DiscountPct, 0) / 100)) AS Revenue
        , COUNT(DISTINCT RECENT.OrderId)                                                AS Orders_N
        , MAX(RECENT.OrderDate)                                                         AS LastOrderDate
     INTO #customer_revenue
     FROM #recent_orders  AS RECENT
     JOIN dbo.order_lines AS LINE
       ON LINE.OrderId = RECENT.OrderId
 GROUP BY RECENT.CustomerId
        ;

DROP TABLE #recent_orders;

   SELECT CUST.CustomerName
        , CUST.Region
        , REV.Revenue
        , REV.Orders_N
        , REV.LastOrderDate
     FROM #customer_revenue AS REV
     JOIN dbo.customers     AS CUST
       ON CUST.CustomerId = REV.CustomerId
    WHERE CUST.Status = 'Active'
 ORDER BY REV.Revenue DESC
        ;

DROP TABLE #customer_revenue;
