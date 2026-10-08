---
title: "SQL Server 2016 Query Checker"
audience: [llm, human]
published: 2026-09-01
subject: "What check_queries.py validates (SQL Server 2016 legality, permitted post-2016 features, house-style decomposition, optional column existence against a metadata corpus), how to run it, and how to read its output."
outcomes:
  - "Run the checker in default efficiency mode, or in --debug house-style mode when asked."
  - "Follow the T-SQL author checklist and the temp-table decomposition approach."
  - "Understand column-existence checks, --assume-exists, the exit codes and the output format."
---

# SQL Server 2016 Query Checker

Checks T-SQL statements for SQL Server 2016 legality before they reach a
server: useful when you write SQL (or an LLM writes it for you) with no
instance to run it against. Python 3.10+ only; sqlglot is vendored.

```sh
python3 tools/sql-server-2016-query-check/check_queries.py FILE.sql
python3 tools/sql-server-2016-query-check/check_queries.py FILE.sql --metadata corpus/   # + column checks
```

- **Efficiency mode is the default.** CTEs, derived tables and subqueries are
  allowed, and `EFF_*` advisories are reported. The house-style temp-table
  decomposition rules are opt-in via **`--debug`**.
- **Column-existence checks are off unless you pass `--metadata DIR`**, a
  corpus built from your own server by [`tools/sqlserver-metadata`](../sqlserver-metadata/README.md).
- Exit code is 1 if any query is invalid, 0 otherwise. Advisories do not
  affect it.

A tool to check the legality of SQL Server 2016 queries using [sqlglot](https://github.com/tobymao/sqlglot).
Efficiency mode is the default. Agents: pass `--debug` only when the user has
**specifically asked** for "house-style" or "debug".

## What it checks

1. **Parse errors** - SQL syntax that cannot be parsed (e.g., missing `SELECT` keywords in subqueries)
2. **Post-2016 features** - Features introduced after SQL Server 2016:
   - `JSON_OBJECT` / `JSON_ARRAY` (2017+)
   - `GRAPH` tables (2017+)
   - `MEMORY_OPTIMIZED` tables (2014+)

   Note: `STRING_AGG` and `WITHIN GROUP` are NOT flagged - see [Permitted "post-2016" features](#permitted-post-2016-features) below.
3. **Column existence** (only with `--metadata DIR`) - every column read from a
   table or view that the corpus describes must exist in that object's catalogue
   entry (`COLUMN_NOT_FOUND`) - see [Column existence checks](#column-existence-checks---metadata)
   **Aliases** - every `ALIAS.column` must name an alias that a `FROM`, `JOIN`,
   `APPLY` or DML target declares in that scope or an enclosing one
   (`UNKNOWN_ALIAS`, an error, whatever the number of sources). The column must
   also exist in the alias's `#temp` table, CTE or derived table when the script
   defines its columns (`ALIAS_COLUMN_NOT_FOUND`) - see
   [Alias column checks](#alias-column-checks-temp-tables-ctes-derived-tables)
4. **T-SQL constraints** - The engine-level rules in
   [T-SQL constraints to respect](#t-sql-constraints-to-respect-author-checklist)
   below (GROUP BY/aggregate scope, window function placement, ORDER BY and
   DISTINCT, subquery arity, UNION arity, recursive CTE shape, temp table
   scope, name resolution, ordering determinism)
5. **House style** - The [decomposition rules](#recommended-decomposition-approach-temp-tables)
   (no CTEs/derived tables/inner queries, compute repeated expressions once,
   drop temp tables at the first opportunity, scan each base table once).
   Enforced only under `--debug`.
6. **Alias naming** - `ALIAS_NOT_DESCRIPTIVE` (_advisory_, both modes): table
   aliases of one or two letters with optional digits (`x`, `V`, `t1`), or
   aliases in one statement that differ only by a trailing number
   (`FAM1`/`FAM2`). See `SQL-STYLE.md` at the repo root. Layout is not checked.
7. **Feature detection** - Identifies SQL Server features used (temp tables, CTEs, FOR XML, etc.)

Findings are either **errors** (they mark the query invalid and set exit code 1)
or **advisories** (reported under `ADVISORIES`, exit code unaffected) for rules
that cannot be decided without schema knowledge. `--list-rules` prints the whole
catalogue with rule ids and severities.

## Permitted "post-2016" features

`STRING_AGG` (and `WITHIN GROUP`) are technically SQL Server 2017+ features, but
many "2016" servers run a newer engine at an older compatibility level, so they
are assumed available. If yours really is 2016, move them from
`ALLOWED_POST_2016_FEATURES` to `POST_SQLSERVER_2016_FEATURES` in the script. The checker treats them as legal: it reports them only as 
detected features in `--verbose` output and never marks a query invalid for using them.

## Recommended decomposition approach (temp tables)

House style (AKA "debug") for complex 2016 queries. Applies only under `--debug`.

- **No inner queries or CTEs.** No derived tables (`FROM (...)`), no
  `IN (SELECT ...)`, `EXISTS (SELECT ...)`, and no `WITH` CTEs. Materialize each
  filtered population into a local temp table (`#table`) and join in the temp
  table to access it. Anti-patterns become `LEFT JOIN ... WHERE x IS NULL`
  (anti-join) or plain `INNER JOIN` against a pre-built distinct-key table.
- **Compute repeated expressions once.** If a computation would run twice -
  typically a `CASE` that appears in both the `SELECT` list and the `GROUP BY`,
  or a filtered population reused by several queries - compute it once, store
  the result as a column of (or row in) a temp table, and let the later
  statements join in that temp table instead of re-deriving it.
- **Drop temp tables at the first opportunity** - immediately after the
  statement that uses one for the last time. Every temp table created in the
  script should be dropped before the script ends.
- **Scan each base table at most once.** Do a single filtered scan of each base
  table into a population temp table and derive all subsets (further filters,
  distincts, joins to other shared temp tables) from that one scan. Repeated
  re-joins happen between temp tables, never against the base table.

These four rules carry the rule ids `DECOMP_*` and are enforced **only under
`--debug`**. In the default mode
the query may be built in whatever way is most efficient - CTEs, derived tables,
`IN (SELECT ...)`, `EXISTS (...)` and repeated expressions are all allowed - and
the checker reports efficiency advisories (`EFF_*`: `SELECT *`, non-sargable
predicates, leading-wildcard `LIKE`, per-row scalar subqueries, cursors/`WHILE`
loops, redundant `DISTINCT` in a `UNION` branch) instead. Pass `--debug` (or the legacy `--optimise false`) to
turn the decomposition rules back on. The T-SQL constraints below are enforced
in **both** modes.

The checker splits the script at lines ending in a code `;` (a `;` inside a
comment or string literal does not split; a trailing `-- comment` after it
does not hide it) and validates each statement
independently, then does a script-level lifecycle pass across the statements in
order: every `#table` must be created before its first reference (`TEMP_SCOPE`,
an error in both modes), dropped before the script ends
(`DECOMP_TEMP_NOT_DROPPED`), dropped once (`DECOMP_TEMP_DROPPED_TWICE`) and
dropped straight after its last use (`DECOMP_DROP_LATE`), and each base table
must be referenced by only one statement (`DECOMP_BASE_TABLE_SCAN`). If the
script consumes temp tables that an earlier script created, declare them with
`--assume-exists '#a,#b'` so they are not reported.

## T-SQL constraints to respect (author checklist)

sqlglot checks syntax, not semantics, so these engine-level T-SQL constraints
are implemented as separate AST checks on top of it. They are enforced in both
modes. Every query (including temp-table-decomposed ones) must satisfy all of
them. Rules the checker cannot decide without the schema (marked
_advisory_ below) are reported but do not fail the run - review those by hand.

### GROUP BY and aggregates

- Grouped expressions must not contain aggregates
- Grouped expressions must not contain subqueries
- Every non-aggregated selected column must appear in the GROUP BY clause
- SELECT aliases must not be referenced in GROUP BY clauses
- Aggregates must not be used in WHERE clauses
- Aggregate expressions must only be referenced within valid aggregate scope
- Aggregates must not be nested inside other aggregates
- Grouping expressions must be deterministic and valid column expressions
  (`GROUP_BY_NONDETERMINISTIC`, _advisory_)
- Aggregates and detail-level columns must not be mixed without appropriate grouping
- HAVING clauses must contain only valid grouped or aggregated expressions
- Legacy `text`/`ntext`/`image` data types cannot be grouped or deduplicated directly

### Window functions

- Window functions must not be used in WHERE clauses
- Window functions must not be used in GROUP BY clauses
- Window functions must not be nested inside other window functions

### ORDER BY and DISTINCT

- ORDER BY clauses in derived tables, CTEs, views, and subqueries require a
  valid companion construct such as `TOP` or `FOR XML`
- ORDER BY expressions used with DISTINCT must be present in the select list

### Subqueries

- Scalar subqueries must return exactly one row and one column (the column
  count is an error; the row count is _advisory_ when there is no aggregate or
  `TOP 1`)
- Correlated subqueries must produce a valid scalar result when used in scalar contexts
- NOT IN predicates must account for the presence of NULL values (a NULL in the
  subquery result makes `NOT IN` match nothing; prefer a LEFT-join anti-pattern)
  (_advisory_)

### UNION and CTEs

- UNION branches must return the same number of columns
- UNION branches must have compatible data types (_advisory_ - only literal
  and `CAST` types can be compared statically)
- Recursive CTE anchor and recursive members must have matching column counts
  and data types
- Recursive CTEs must contain both anchor and recursive members
- Recursive CTEs must not contain disallowed constructs

### Data type compatibility

- Expressions must return compatible data types across CASE branches
  (_advisory_)
- Expressions must return compatible data types across COALESCE and ISNULL
  arguments (_advisory_)

### Scope and name resolution

- Temporary objects must exist within the same execution scope in which they
  are referenced (local `#table` objects belong to one session; a temp table is
  usable only after the statement that creates it has run)
- Object and column names must resolve at compile time or via deferred name
  resolution rules
- Multi-part identifiers must bind to valid objects and aliases
- Column references must be unambiguous (qualify with a table alias when the
  same column exists in more than one source) (_advisory_ - the checker flags
  unqualified columns whenever two or more sources are in scope)
- Aliases must only be referenced within their valid scope

### XML, ordering and result determinism

- XML string aggregation logic must account for XML encoding and length
  limitations (relevant when using the `FOR XML PATH` fallback for STRING_AGG)
  (_advisory_)
- Query logic must not rely on row ordering unless ordering is explicitly
  guaranteed (add `ORDER BY`; for `STRING_AGG`, use
  `WITHIN GROUP (ORDER BY ...)` whenever the output order matters) (_advisory_)

## Column existence checks (`--metadata`)

Off unless `--metadata DIR` is given. Build `DIR` from your own server with
[`tools/sqlserver-metadata`](../sqlserver-metadata/README.md). Every `TABLE` and
`VIEW` under it is indexed into a
catalogue of `database.schema.object` -> column list. Whenever a query reads a
table or view **that the catalogue describes**, every column it reads from that
object must appear in that object's catalogue entry; anything else is reported
as `COLUMN_NOT_FOUND` (an error, in both modes):

```
ERROR [COLUMN_NOT_FOUND] [shop].[dbo].[orders]
      (table, 6 columns in metadata) has no column: OrderStatusCode
      (did you mean 'OrderStatus'?) - first referenced in query #1
```

Columns are read from the parsed statement, not from raw text, so the check
knows which source each reference belongs to:

- **Qualified references** (`alias.column`, `schema.table.column`) are checked
  against whatever that alias resolves to in the innermost scope that defines
  it - correlated subqueries included.
- **Unqualified references** are checked only when the enclosing scope reads
  exactly one source, so an unqualified name is unambiguous.
- `INSERT INTO t (a, b)` column lists are checked against `t`.

Table names are resolved however the query writes them: three-part
(`[db].[schema].[obj]`), two-part (`[schema].[obj]`) or bare. A partial name
can match several catalogued objects, and the column then only has to exist in
one of them.

Nothing is reported for sources whose columns cannot be known:

- objects the catalogue does not describe (other systems, other databases)
- temp tables (`#t`, `##t`), table variables, CTEs, derived tables, table-valued
  functions and `sys` / `INFORMATION_SCHEMA` objects
- catalogued objects whose own column list could not be read - a view defined
  as `SELECT *`, or a definition sqlglot cannot parse. These stay in the
  catalogue as known names, but never produce a `COLUMN_NOT_FOUND`. The startup
  line reports how many objects are in that state:

```
Indexed 5 tables/views (5 with a readable column list, 0 without)
```

## Alias column checks (#temp tables, CTEs, derived tables)

`ALIAS_COLUMN_NOT_FOUND` (an error) covers the sources the corpus cannot
describe. The script is read in order, and each `#temp` table takes its columns
from the statement that last defined it before the reference:

- `CREATE TABLE #t (a int, ...)`, `SELECT ... INTO #t` (the select-list names),
  and `ALTER TABLE #t ADD c ...`
- a CTE's column list `WITH C (a, b)` or else its select list, and a derived
  table's `AS D (a, b)` or else its select list

```
ERROR [ALIAS_COLUMN_NOT_FOUND] query #2: LATE.OrderDte (did you mean 'orderdate'?)
      - #late_orders (created in query #1) has no column OrderDte; it has: orderdate, orderid, orderstatuscode
```

`INSERT INTO #t (a, b)` column lists are checked as well. A source is not
checked when its columns cannot be known: a `#temp` table created in another
file (`--assume-exists`) or in dynamic SQL, one built with `SELECT *` or with an
unnamed column, and one created by a statement that did not parse. Only
qualified references are checked.

**Batch separators.** A `GO` line ends the statement before it and is dropped,
so a statement that follows `GO` with no `;` between them is still checked.

## Usage

```bash
# Basic usage - efficiency mode (default): CTEs allowed, EFF_* advisories on
python3 check_queries.py <sql_file>

# House-style temp-table decomposition rules
python3 check_queries.py <sql_file> --debug

# Verbose output (shows features and every finding for all queries)
python3 check_queries.py <sql_file> --verbose

# Stop at first invalid query
python3 check_queries.py <sql_file> --fail-fast

# Legacy spellings, still accepted
python3 check_queries.py <sql_file> --optimise           # = the default
python3 check_queries.py <sql_file> --optimise false     # = --debug

# Temp tables created by an earlier script
python3 check_queries.py <sql_file> --assume-exists '#recent_orders,#top_customers'

# Print the rule catalogue (ids, severities, which mode they belong to)
python3 check_queries.py --list-rules
```

## Example

```bash
# from the repo root: the demo schema ships as a tiny corpus
python3 tools/sql-server-2016-query-check/check_queries.py \
    examples/sales_by_customer.sql --metadata examples/metadata --verbose   # 7 valid

# both errors above come from this one
python3 tools/sql-server-2016-query-check/check_queries.py \
    examples/bad_columns.sql --metadata examples/metadata                   # exit 1
```

## Exit codes

- `0` - All queries are valid (advisories may still be reported)
- `1` - One or more queries have errors, or a script-level check failed

## Dependencies

- Python 3.10+
- sqlglot 30.17.0, vendored in `_vendor/` (MIT, see its `LICENSE`); nothing to install

## Output format

The tool reports:
- Per-query VALID/INVALID status
- Parse error details with line numbers
- Rule violations, tagged with their rule id (e.g. `[GROUP_BY_ALIAS]`)
- Advisories, listed separately; they never change the exit code
- Script-level checks (temp table lifecycle, base table scan counts)
- Detected SQL Server features
- Feature version warnings
