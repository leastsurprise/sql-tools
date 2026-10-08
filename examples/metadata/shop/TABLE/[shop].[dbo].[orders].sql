-- Database: shop
-- Object: [shop].[dbo].[orders]
-- Type: TABLE
-- Timestamp: 1791504000
-- Source: metadata.csv
================================================================================
CREATE TABLE [dbo].[orders]
(
    [OrderId] INT NOT NULL,
    [CustomerId] INT NOT NULL,
    [OrderDate] DATE NOT NULL,
    [OrderStatus] VARCHAR(12) NOT NULL,
    [ShippedDate] DATE NULL,
    [ChannelCode] VARCHAR(10) NOT NULL
);
