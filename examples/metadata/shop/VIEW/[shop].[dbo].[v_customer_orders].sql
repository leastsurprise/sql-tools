-- Database: shop
-- Object: [shop].[dbo].[v_customer_orders]
-- Type: VIEW
-- Timestamp: 1791504000
-- Source: metadata.csv
================================================================================
CREATE VIEW dbo.v_customer_orders AS
SELECT c.CustomerId, c.CustomerName, o.OrderId, o.OrderDate
FROM dbo.customers AS c JOIN dbo.orders AS o ON o.CustomerId = c.CustomerId;
