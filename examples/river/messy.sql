-- The same kind of SQL as case_and_calls.sql, written carelessly. river.py settles it.
SELECT
ORD.OrderId,
  ORD.CustomerId
, CASE WHEN ORD.ChannelCode = 'WEB'
  AND ORD.ShippedDate IS NOT NULL THEN 1
  ELSE 0 END AS ShippedOnline
, ROW_NUMBER() OVER (
  PARTITION BY ORD.CustomerId
  ORDER BY ORD.OrderDate
  ) AS CustomerOrderRank
FROM dbo.orders AS ORD
  LEFT JOIN dbo.customers AS CUST
ON CUST.CustomerId = ORD.CustomerId
WHERE
ORD.OrderStatus <> 'Cancelled'
    AND CUST.Region = 'North'
ORDER BY ORD.OrderId
;
