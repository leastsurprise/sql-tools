-- Deliberately wrong: run with --metadata examples/metadata to see the column checks.
   SELECT ORD.OrderId
        , ORD.OrderStatusCode
        , ORD.OrderDate
     INTO #late_orders
     FROM dbo.orders AS ORD
    WHERE ORD.ShippedDate IS NULL
        ;

   SELECT LATE.OrderId
        , LATE.OrderDte
     FROM #late_orders AS LATE
        ;

DROP TABLE #late_orders;
