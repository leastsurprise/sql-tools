-- Database: shop
-- Object: [shop].[dbo].[customers]
-- Type: TABLE
-- Timestamp: 1791504000
-- Source: metadata.csv
================================================================================
CREATE TABLE [dbo].[customers]
(
    [CustomerId] INT NOT NULL,
    [CustomerName] NVARCHAR(200) NOT NULL,
    [Email] NVARCHAR(320) NULL,
    [Phone] VARCHAR(30) NULL,
    [Region] VARCHAR(20) NOT NULL,
    [Status] VARCHAR(10) NOT NULL,
    [CreatedDate] DATE NOT NULL
);
