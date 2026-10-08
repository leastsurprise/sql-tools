    SELECT CUST_ROWS.CustomerId
         , CUST_ROWS.CustomerName
         , CUST_ROWS.Email
         , CUST_ROWS.Phone
         , CUST_ROWS.Region
         , CUST_ROWS.Status
         , CUST_ROWS.CreatedDate
         , CUST_ROWS.FirstOrderDate
         , CUST_ROWS.LastOrderDate
         , CUST_ROWS.Orders_N
         , CUST_ROWS.Revenue
         , CUST_ROWS.ReturnsRate
      INTO #retention_pool
      FROM #customer_rows AS CUST_ROWS
     WHERE CUST_ROWS.Status = 'Active'
           -- The retention pool, as the sales team defines it:
           --   'Active'               the account is open
           --   'Gold' / 'Silver'      the two loyalty tiers in scope
           --   LastOrderDate IS NULL  or a lapsed buyer, see below
           -- Switch Silver off with @IncludeSilver = 0.
       AND (CUST_ROWS.LoyaltyTier = 'Gold'
            OR (@IncludeSilver = 1 AND CUST_ROWS.LoyaltyTier = 'Silver')
           )
       AND CUST_ROWS.ClosedDate IS NULL
           -- '<> Blocked' is NULL-unsafe and would drop customers with no flag row;
           -- the IS NULL branch keeps "unknown" in.
       AND (CUST_ROWS.CreditFlag IS NULL
            OR CUST_ROWS.CreditFlag <> 'Blocked'
           )
         ;
