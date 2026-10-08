---
title: "river-layout - keyword-river formatter for SQL-STYLE.md layout"
audience: [human, llm]
published: 2026-10-05
subject: "river.py, the one-shot entry point that formats, proves the code unchanged and logs what it cannot settle; then the two passes that put a T-SQL file on the SQL-STYLE.md keyword river per scope (river, argument column, comments, CASE, bracket mirroring), then align the AS column, plus a read-only audit that measures the result."
outcomes:
  - "Format a .sql file's governed statements onto the river without changing a token, a literal or a comment's text."
  - "Run the passes once each and read a second run's '0 lines moved' as the proof the file is settled."
  - "Know the scope model (statement, paren pair, CASE) the passes and the audit share, and the invariants I1-I6 it enforces."
  - "Use _audit_layout.py as the layout gate and know what it does not judge."
---

# river-layout

> **Working sessions: run one command, once, and do not read further.**
>
> ```sh
> python3 tools/river-layout/river.py FILE.sql      # Pass 1 + Pass 2 + proof + gate
> ```
>
> It prints `settled` or `LOGGED`. `LOGGED` means the passes could not settle the
> file, crashed, or would have changed code. The input and the evidence are saved to
> `issues/ISSUES.md` for a dedicated session, and the file is left legal. Your layout
> step is finished either way: ship once `check_queries.py` passes. The rest of this
> README is for whoever fixes the passes: start from `issues/ISSUES.md`, re-run each
> saved input, and delete the entries that now settle. `issues/` is git-ignored,
> because it holds copies of your own SQL.

[`SQL-STYLE.md`](../../SQL-STYLE.md) prescribes a layout the query checker does not
enforce. Two passes do the mechanical part and a
third measures it. All three share one scope model, `_river_core.py`.

```sh
python3 tools/river-layout/_river_layout.py --report FILE.sql   # what it would move, writes nothing
python3 tools/river-layout/_river_layout.py          FILE.sql   # Pass 1: river, argument column, comments
python3 tools/river-layout/_align_items.py           FILE.sql   # Pass 2: the AS column
python3 tools/river-layout/_audit_layout.py          FILE.sql   # Pass 3: findings, read-only
python3 tools/sql-server-2016-query-check/check_queries.py FILE.sql   # verify: 0 invalid
```

**Both passes are idempotent.** Run each once; a second run of Pass 1 that prints
`moved 0 lines, joined 0` and of Pass 2 that prints `0 AS aliases aligned` is the proof
the file is settled. (Before 5 Oct 2026 Pass 1 needed 2-3 runs, because it measured
nested scopes from the text before it had moved it. It now places a scope's lines only
after the line that opens the scope is placed.)

## The scope model - `_river_core.py`

A statement is split on `;` at paren depth 0, with block comments, line comments and
string literals blanked first - including literals that span lines, such as dynamic SQL
in `EXEC (N'...')` - the query checker's convention, so a prose `;` cannot slice a
statement.

A statement is **governed** when its first code line starts with `SELECT`, `WITH`,
`INSERT`, `UPDATE` or `DELETE`. Everything else - DDL, `IF` guards (even one wrapping a
`SELECT`), `EXEC`, `DECLARE`, `SET`, `DROP`, header boilerplate - keeps the layout it
was written with, in all three tools.

Inside a governed statement, every line belongs to the innermost scope open at its start:

| Scope   | What opens it                     | base (where the longest keyword starts)                     |
| ------- | --------------------------------- | ----------------------------------------------------------- |
| `stmt`  | the statement                     | 4 per open `BEGIN` block (4 in a proc body), else as authored at file level |
| `paren` | a `(` whose `)` is on a later line | one column right of the `(`, measured after its line is placed |
| `case`  | a `CASE` whose `END` is on a later line | the `CASE` token's column; the river is 4 wide          |

Scopes are keyed on the **pair**, not on paren depth, so two sibling subqueries at one
depth each get their own base. Per scope: `river = base + width - 1` (width = the
longest keyword in the scope), and the argument column `A = river + 2` (= `base` when the
scope has no keyword, e.g. a function's argument list with no comma line).

## Pass 1 - `_river_layout.py`

| #   | Rule                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------ |
| I1  | A keyword never stands alone: a bare `SELECT`/`WHERE`/`ON`/`AND` takes its next code line as its       |
|     | argument, and any comment lines in between stay, below the joined line. `CASE`, `END`, `;` and         |
|     | `UNION` stand alone by design                                                                          |
| -   | Every keyword in a scope right-aligns to the scope's river                                              |
| I2  | Every argument, continuation line and nested-scope opener starts at `A`                                |
| I3  | A line that opens with `)` sits in the column of the `(` it closes                                     |
| I4  | The comment block directly above a statement left-aligns to the statement's first keyword              |
| I5  | A comment line inside a scope sits at that scope's `A`                                                 |
| I6  | `WHEN`/`THEN`/`ELSE`/`END`, and `AND`/`OR` inside a `WHEN`, right-align to the `CASE` river           |

Whitespace only: indentation changes, and an I1 join removes a line break. No token,
literal or comment text changes; block comments and lines inside a multi-line string
literal never move. A trailing comment travels with its line.

## Pass 2 - `_align_items.py`, the AS column

Per scope, every `expr AS alias` line puts its `AS` one space past the longest expression
in the scope (a trailing comment does not count). A line that opens with the `)` or `END`
closing a nested scope - `) AS x`, `END AS x` - is an item of the enclosing scope, so a
multi-line `CASE`, call or subquery takes its alias in the same column as the one-line
items around it. Only the gap before `AS` changes, and Pass 1 never reads line length, so
the two passes do not fight.

## Pass 3 - `_audit_layout.py`, the symmetry check

Read-only, on governed statements only, per scope of the model above. Prints
`file:line: SYMnn detail`, per finding or with `--summary`:

| Code  | Measures                                                                                |
| ----- | --------------------------------------------------------------------------------------- |
| SYM01 | a scope keyword with no argument on its own line (a lone `SELECT`/`WHERE`/`ON`/`AND`)   |
| SYM02 | more than one column starting arguments in one scope                                    |
| SYM03 | a `)` that leads its line, not in the column of the `(` it closes                       |
| SYM04 | keywords of one scope ending in more than one column - more than one river              |
| SYM05 | a comment block introducing a statement, not left-aligned to its first keyword          |
| SYM06 | a comment inside a scope, not at that scope's argument column                           |
| SYM07 | a tab in the indentation of a line outside a block comment                              |

It is a text model, not a parser: treat a count as a ranking, not a verdict. The alias
after `) AS x` / `END AS x` belongs to the enclosing item and is not counted as an
argument of the scope being closed.

## Checks

* `examples/river/settled_select_into.sql` (a long `SELECT ... INTO` with commented
  `AND (... OR ...)` predicates) and `examples/river/case_and_calls.sql` (CASE with `AND`,
  nested calls, `OVER (...)`, a joined subquery, bare `SELECT`/`WHERE`) are fixed points
  of both passes with 0 audit findings and 0 invalid in the checker. Run the passes over a
  copy of each after any change to these tools; `diff` must be empty.
* `examples/river/messy.sql` is the same kind of SQL written carelessly: one run of
  `river.py` settles it, a second run moves nothing, and the proof shows the code text
  unchanged.

## What they do not do

* they do not check aliases - `ALIAS_NOT_DESCRIPTIVE` is the checker's job
* content written after a `(` on the opener's line - `CONCAT(CAST(...)`, `AND (x = 1` -
  stays glued to the `(`, one column left of the lines after it that start with a keyword
* they do not bound nesting: a scope opened by a `(` far right
  lays out far right. Break the expression (a `#temp`, a CTE) instead
* they do not fix a `CASE` closed with `)`, a missing `END` or a comma in the wrong
  place, and a statement with unbalanced brackets is left exactly as written
* they do not wrap, reflow or re-wrap comments
