---
title: "SQL-STYLE.md — layout and naming"
audience: [llm, human]
published: 2026-10-09
subject: "How SQL is written with these tools: the reuse ladder, role-suffixed aliases and the right-aligned keyword river, with a worked example."
outcomes:
  - "Stop at the first rung of the reuse ladder that holds, before deciding what to write."
  - "Name table aliases descriptively, with role suffixes rather than numbers, and always with AS."
  - "Lay out every scope as a right-aligned keyword river."
  - "Know which rule the checker enforces (ALIAS_NOT_DESCRIPTIVE), and that river.py's single run is the whole layout step."
---

# SQL-STYLE.md — layout and naming for SQL you write

Applies to **every** statement you write, in default and house-style (`--debug`)
mode alike. It does not apply to SQL you quote from elsewhere, such as a
procedure body copied from a server. Leave that as written.

The checker enforces the alias rule (`ALIAS_NOT_DESCRIPTIVE`, a WARNING, in both
modes). It does not check the layout. Write close to the river and run
`python3 tools/river-layout/river.py FILE.sql` once. Layout is the 90% of the
work that buys 10% of the value: when `river.py` prints `LOGGED`, ship the file
as it is, legal but ugly, and do not hand-align it.

## The reuse ladder

Decide **what to write** before deciding how to write it. Stop at the first rung
that holds. The ladder runs *after* you understand the question (read the
procedures and views the answer touches, follow each column back through every
hop, resolve every identifier), never instead of it. It shortens the solution,
never the reading.

| Rung | Ask                                | What it means                                                                                  |
| ---- | ---------------------------------- | ---------------------------------------------------------------------------------------------- |
| 1    | Does it need to exist at all?      | The request asked for a number. A staging procedure, a wrapper view, a check table, a          |
|      |                                    | `#params` column nothing reads, a second output "while we're here": skip it and say so.        |
| 2    | **Does the database already do     | A live view or procedure may already compute this. Search the object definitions (a            |
|      | it?**                              | `--metadata` corpus is greppable) before writing the calculation. Highest-value rung.          |
| 3    | Does the engine do it?             | Window functions, `STRING_AGG ... WITHIN GROUP`, `STRING_SPLIT`, `CROSS`/`OUTER APPLY`,        |
|      |                                    | `ISNULL`. Do not hand-roll a rank, a dedupe, a running total, a split.                         |
| 4    | Does the reporting model do it?    | A relationship and a measure, a report filter, a role on the date table. Do not re-implement   |
|      |                                    | per refresh what the semantic model does once.                                                 |
| 5    | Does a script in `tools/` do it?   | `check_queries.py`, `river-layout/`. No new dependency.                                        |
| 6    | Is one statement enough?           | The answer is often one `SELECT`. Ship that first, then the scaffolding if it is needed.       |
| 7    | Only then                          | The minimum that works **in house style**: a `#temp` per population, `#params`, the river,     |
|      |                                    | descriptive aliases.                                                                           |

Rung 2 never licenses an assumption. "The view probably does it" is a claim:
open it, read the body, match the filter, cite where it came from. A dated
backup copy of a procedure is never the live object.

**Never lazy about** NULL and duplicate handling, an explicit list of
assumptions, and the `check_queries.py` run. When house style is requested, the
shortest diff never wins against it: the `DECOMP_*` rules under `--debug` are
the point of the SQL, not bloat in it.

A deliberate shortcut that cuts a real corner carries a marker naming its
ceiling and the trigger to revisit, so "later" does not become "never":

```sql
-- lazy: naive scan of #recent_orders, revisit above ~100k rows
```

`grep -rn -- '-- lazy:'` finds them all.

## Table aliases

- **Descriptive, never terse.** No `x`, `x1`, `y`, `t2`, `E`, `V`, `p`. Build
  the alias from the table's name or what it does there: upper case, words
  separated by `_`, vowels dropped as needed to keep it readable.
  `order_lines` → `ORD_LINE`; `#params` → `PARAMS`.
- **A table used more than once gets a role suffix**, not a number. Two joins to
  `customers` (the buyer and the person billed) are `CUST_BUYER` and
  `CUST_BILLING`, never `CUST1` and `CUST2`. Name what each copy *is* in this
  query.
- Always write `AS` before a table alias.

What the checker flags: an alias of one or two letters, optionally followed by
digits (`x`, `V`, `ab`, `t1`), and two or more aliases in one statement that
differ only by a trailing number (`CUST1`/`CUST2`, `ORD_1`/`ORD_2`). Passing it
does not make an alias good. `TBL_A` passes and is still meaningless.

## Layout: the keyword river

Every formatting scope is a list of *(keyword, argument)* pairs:

| Scope     | Keywords                                                                                      |
| --------- | --------------------------------------------------------------------------------------------- |
| SELECT    | `SELECT`, `,` (each further select item), `INTO`, `FROM`,                                     |
|           | `[INNER\|LEFT\|RIGHT\|FULL OUTER\|CROSS] JOIN`, `ON`, `WHERE`, `GROUP BY`, `HAVING`,          |
|           | `ORDER BY`, `;`                                                                               |
| Predicate | `WHERE`/`ON`/`HAVING`, `AND`, `OR`                                                            |
| CASE      | `CASE`, `WHEN`, `THEN`, `ELSE`, `END`                                                         |

For each scope:

1. Find the longest keyword in the scope.
2. Right-align every sibling keyword to it (pad on the left).
3. Emit one space.
4. Emit the formatted argument.
5. Recurse into nested scopes.

```
def format_node(node):
    if node.establishes_alignment_scope():
        children = node.keyword_argument_children()
        max_len = max(len(child.keyword) for child in children)
        for child in children:
            emit(pad_left(child.keyword, width=max_len))
            emit(" ")
            format_node(child.argument)
    else:
        emit(render_leaf(node))
```

What this means in practice:

- **The longest keyword in the statement sets the river.** `LEFT JOIN` (9) puts
  every keyword's last letter in column 9. If the statement has a
  `FULL OUTER JOIN`, everything pads to 15.
- **One select item per line, leading comma.** The `,` is a keyword, so it sits
  right-aligned on the river.
- **`ON`, `AND`, `OR` sit on the same river** as the clause they continue.
- **The terminating `;` goes on its own line**, right-aligned on the river.
- **A nested scope** (a `CASE` inside `SUM(`, a subquery inside `(`) starts on a
  new line, one column right of the opening `(`, and aligns its own keywords
  from there. The closing `)` goes directly under the opening `(`.
- **`CASE` blocks right-align too.** `CASE`, `WHEN`, `THEN` and `ELSE` are all
  four letters, so `END` gets one space of padding to end in the same column.
  Each keyword takes its own line.
- **Every `AS alias` lines up in one column** across the statement, in both
  the select list and the `FROM`/`JOIN` lines. Put it one space past the
  longest expression.
- Keywords and built-in function names are upper case.
- No trailing whitespace.

Statements without a SELECT body (`DROP TABLE`, `EXEC`, a one-line `INSERT`)
follow the same idea: one keyword per line where there is more than one
clause, right-aligned to the longest.

## Worked example

Spaces shown as `^`:

```
^^^SELECT^ISNULL(CUST.Region,^'NO^REGION')^AS^CustomerRegion
^^^^^^^^,^COUNT(DISTINCT^ORD.OrderId)^^^^^^AS^Orders_N
^^^^^^^^,^SUM(
^^^^^^^^^^^^^^CASE
^^^^^^^^^^^^^^WHEN^ORD.ShippedDate^IS^NOT^NULL
^^^^^^^^^^^^^^THEN^1
^^^^^^^^^^^^^^ELSE^0
^^^^^^^^^^^^^^^END
^^^^^^^^^^^^^)^^^^^^^^^^^^^^^^^^^^^^^^^^^^^AS^ShippedOrders_N
^^^^^FROM^dbo.orders^^^^^^^^^^^^^^^^^^^^^^^AS^ORD
LEFT^JOIN^dbo.customers^^^^^^^^^^^^^^^^^^^^AS^CUST
^^^^^^^ON^CUST.CustomerId^=^ORD.CustomerId
^^^^WHERE^ORD.ChannelCode^IN^('WEB',^'APP')
^^^^^^AND^ORD.OrderStatus^<>^'Cancelled'
^^^^^^AND^ORD.OrderDate^>=^'2026-07-01'
^GROUP^BY^ISNULL(CUST.Region,^'NO^REGION')
^ORDER^BY^2^DESC
^^^^^^^^;
```

As it is written:

```sql
   SELECT ISNULL(CUST.Region, 'NO REGION') AS CustomerRegion
        , COUNT(DISTINCT ORD.OrderId)      AS Orders_N
        , SUM(
              CASE
              WHEN ORD.ShippedDate IS NOT NULL
              THEN 1
              ELSE 0
               END
             )                             AS ShippedOrders_N
     FROM dbo.orders                       AS ORD
LEFT JOIN dbo.customers                    AS CUST
       ON CUST.CustomerId = ORD.CustomerId
    WHERE ORD.ChannelCode IN ('WEB', 'APP')
      AND ORD.OrderStatus <> 'Cancelled'
      AND ORD.OrderDate >= '2026-07-01'
 GROUP BY ISNULL(CUST.Region, 'NO REGION')
 ORDER BY 2 DESC
        ;
```

The house-style `#params` table, in this style:

```sql
   SELECT CAST(1 AS int)             AS idx
        , CAST('2026-07-01' AS date) AS StartDate
        , CAST(123 AS int)           AS CustomerId
     INTO #params
        ;
```

joined as `INNER JOIN #params AS PARAMS ON PARAMS.idx = 1`.
