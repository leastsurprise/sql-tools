-- Database: shop
-- Object: [shop].[dbo].[order_lines]
-- Type: TABLE
-- Timestamp: 1791504000
-- Source: metadata.csv
================================================================================
CREATE TABLE [dbo].[order_lines]
(
    [OrderLineId] INT NOT NULL,
    [OrderId] INT NOT NULL,
    [ProductId] INT NOT NULL,
    [Quantity] INT NOT NULL,
    [UnitPrice] DECIMAL(10,2) NOT NULL,
    [DiscountPct] DECIMAL(5,2) NULL
);
