-- Database: shop
-- Object: [shop].[dbo].[products]
-- Type: TABLE
-- Timestamp: 1791504000
-- Source: metadata.csv
================================================================================
CREATE TABLE [dbo].[products]
(
    [ProductId] INT NOT NULL,
    [ProductName] NVARCHAR(200) NOT NULL,
    [Category] VARCHAR(40) NOT NULL,
    [UnitPrice] DECIMAL(10,2) NOT NULL
);
