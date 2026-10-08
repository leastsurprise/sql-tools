#!/usr/bin/env python3
"""
SQL Server 2016 Query Legality Checker
=======================================
Parses T-SQL queries using sqlglot and checks for:
  1. Parse errors (syntax that sqlglot cannot handle)
  2. SQL Server 2016+ feature violations (features introduced after 2016)
  3. Column existence (only with --metadata DIR): every column read from a
     table or view described by the metadata corpus must exist in its entry
  4. Engine-level T-SQL constraints (the "author checklist" in README.md)
  5. House-style decomposition rules (temp tables, no inner queries / CTEs),
     enforced only when --debug is given

Usage:
    python3 check_queries.py <sql_file>
    python3 check_queries.py <sql_file> --verbose
    python3 check_queries.py <sql_file> --fail-fast
    python3 check_queries.py <sql_file> --debug               # house-style rules
    python3 check_queries.py --list-rules
"""

import sys
import difflib
import re
from pathlib import Path as _Path

# Use the pinned sqlglot vendored beside this script, so the checker is
# self-contained and gives identical results regardless of what is installed.
# Without this the import fails unless a venv happens to provide sqlglot.
_VENDOR = _Path(__file__).resolve().parent / "_vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))

import sqlglot
from sqlglot import exp
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple
from pathlib import Path

DIALECT = "tsql"

ERROR = "error"
WARNING = "warning"


@dataclass
class Violation:
    rule: str
    message: str
    severity: str = ERROR

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


@dataclass
class QueryResult:
    query_num: int
    query_text: str
    parse_error: Optional[str] = None
    features: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    violations: List[Violation] = field(default_factory=list)
    is_valid: bool = True

    @property
    def errors(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == ERROR]

    @property
    def advisories(self) -> List[Violation]:
        return [v for v in self.violations if v.severity != ERROR]


@dataclass
class CheckReport:
    total_queries: int = 0
    valid_queries: int = 0
    invalid_queries: int = 0
    results: List[QueryResult] = field(default_factory=list)
    script_violations: List[Violation] = field(default_factory=list)

    @property
    def script_errors(self) -> List[Violation]:
        return [v for v in self.script_violations if v.severity == ERROR]



# Features introduced AFTER SQL Server 2016 (NOT available in 2016)
# STRING_AGG and WITHIN GROUP (2017) are deliberately NOT flagged: see
# ALLOWED_POST_2016_FEATURES.
POST_SQLSERVER_2016_FEATURES = {
    "JSON_OBJECT": "JSON_OBJECT (introduced in SQL Server 2017)",
    "JSON_ARRAY": "JSON_ARRAY (introduced in SQL Server 2017)",
    "GRAPH TABLES": "GRAPH tables (introduced in SQL Server 2017)",
    "MEMORY_OPTIMIZED": "MEMORY_OPTIMIZED tables (introduced in SQL Server 2014)",
}

# Features that are technically post-2016 but are assumed available (many
# "2016" servers actually run a newer engine at an older compatibility level).
# Move an entry to POST_SQLSERVER_2016_FEATURES if your server really is 2016.
ALLOWED_POST_2016_FEATURES = {
    "STRING_AGG": "STRING_AGG (2017, assumed available)",
    "WITHIN GROUP": "WITHIN GROUP (2017, assumed available)",
}

# Rule catalogue: id -> (category, default severity, description)
RULES: Dict[str, Tuple[str, str, str]] = {
    # --- GROUP BY and aggregates -------------------------------------------
    "GROUP_BY_AGGREGATE": ("group-by", ERROR, "Grouped expressions must not contain aggregates"),
    "GROUP_BY_SUBQUERY": ("group-by", ERROR, "Grouped expressions must not contain subqueries"),
    "GROUP_BY_WINDOW": ("group-by", ERROR, "Window functions must not be used in GROUP BY clauses"),
    "GROUP_BY_ALIAS": ("group-by", ERROR, "SELECT aliases must not be referenced in GROUP BY clauses"),
    "GROUP_BY_MISSING_COLUMN": ("group-by", ERROR, "Every non-aggregated selected column must appear in the GROUP BY clause"),
    "MIXED_AGG_DETAIL": ("group-by", ERROR, "Aggregates and detail-level columns must not be mixed without grouping"),
    "AGG_IN_WHERE": ("group-by", ERROR, "Aggregates must not be used in WHERE clauses"),
    "NESTED_AGGREGATE": ("group-by", ERROR, "Aggregates must not be nested inside other aggregates"),
    "AGG_OUT_OF_SCOPE": ("group-by", ERROR, "Aggregate expressions must only be referenced within valid aggregate scope"),
    "HAVING_INVALID": ("group-by", ERROR, "HAVING clauses must contain only grouped or aggregated expressions"),
    "GROUP_BY_NONDETERMINISTIC": ("group-by", WARNING, "Grouping expressions must be deterministic"),
    "LEGACY_TYPE_GROUPED": ("group-by", ERROR, "text/ntext/image cannot be grouped or deduplicated directly"),
    # --- Window functions ---------------------------------------------------
    "WINDOW_IN_WHERE": ("window", ERROR, "Window functions must not be used in WHERE clauses"),
    "NESTED_WINDOW": ("window", ERROR, "Window functions must not be nested inside other window functions"),
    # --- ORDER BY and DISTINCT ---------------------------------------------
    "NESTED_ORDER_BY": ("order-by", ERROR, "ORDER BY in a derived table/CTE/view/subquery needs TOP, OFFSET or FOR XML"),
    "DISTINCT_ORDER_BY": ("order-by", ERROR, "ORDER BY expressions used with DISTINCT must be in the select list"),
    "ORDER_NOT_GUARANTEED": ("order-by", WARNING, "Result ordering is not guaranteed (add ORDER BY / WITHIN GROUP (ORDER BY ...))"),
    # --- Subqueries ---------------------------------------------------------
    "SCALAR_SUBQUERY_COLUMNS": ("subquery", ERROR, "Scalar subqueries must return exactly one column"),
    "SCALAR_SUBQUERY_ROWS": ("subquery", WARNING, "Scalar subquery is not guaranteed to return one row (no aggregate / TOP 1)"),
    "NOT_IN_NULL": ("subquery", WARNING, "NOT IN over a subquery matches nothing if the subquery yields NULL; prefer a LEFT JOIN anti-pattern"),
    # --- UNION and CTEs -----------------------------------------------------
    "UNION_COLUMN_COUNT": ("set-op", ERROR, "UNION branches must return the same number of columns"),
    "UNION_TYPE_COMPAT": ("set-op", WARNING, "UNION branches must have compatible data types"),
    "RECURSIVE_CTE_MEMBERS": ("set-op", ERROR, "Recursive CTEs must contain both anchor and recursive members"),
    "RECURSIVE_CTE_DISALLOWED": ("set-op", ERROR, "Recursive CTEs must not contain disallowed constructs"),
    # --- Data type compatibility -------------------------------------------
    "CASE_TYPE_COMPAT": ("types", WARNING, "CASE branches must return compatible data types"),
    "COALESCE_TYPE_COMPAT": ("types", WARNING, "COALESCE/ISNULL arguments must return compatible data types"),
    "LEGACY_TYPE": ("types", WARNING, "Legacy text/ntext/image data type used"),
    # --- Scope and name resolution -----------------------------------------
    "TEMP_SCOPE": ("scope", ERROR, "Temporary objects must be created before they are referenced"),
    "AMBIGUOUS_COLUMN": ("scope", WARNING, "Column references must be unambiguous (qualify with a table alias)"),
    "UNKNOWN_ALIAS": ("scope", ERROR, "Multi-part identifiers must bind to a valid source or alias"),
    # --- Style (both modes; see SQL-STYLE.md) --------------------------------
    "ALIAS_NOT_DESCRIPTIVE": ("style", WARNING, "Table aliases must be descriptive (e.g. CUST_BILLING, not C or x1); repeated tables get a role suffix, not a number"),
    # --- XML ----------------------------------------------------------------
    "XML_ENCODING": ("xml", WARNING, "FOR XML PATH aggregation must handle XML encoding (use , TYPE).value('.', 'nvarchar(max)'))"),
    # --- House-style decomposition (default mode only) ----------------------
    "DECOMP_CTE": ("decomposition", ERROR, "No WITH CTEs - materialize the population into a #temp table"),
    "DECOMP_DERIVED_TABLE": ("decomposition", ERROR, "No derived tables (FROM (SELECT ...)) - use a #temp table and join to it"),
    "DECOMP_SUBQUERY": ("decomposition", ERROR, "No inner queries (IN/EXISTS/scalar subqueries) - use a #temp table join or a LEFT JOIN anti-join"),
    "DECOMP_REPEATED_EXPR": ("decomposition", ERROR, "Expression computed twice (SELECT list and GROUP BY) - compute it once into a #temp table column"),
    "DECOMP_BASE_TABLE_SCAN": ("decomposition", ERROR, "Scan each base table at most once into a population #temp table"),
    "DECOMP_TEMP_NOT_DROPPED": ("decomposition", ERROR, "Every #temp table must be dropped before the script ends"),
    "DECOMP_TEMP_DROPPED_TWICE": ("decomposition", WARNING, "#temp table dropped more than once after creation"),
    "DECOMP_DROP_LATE": ("decomposition", WARNING, "Drop each #temp table immediately after its last use"),
    # --- Efficiency advisories (default mode only) --------------------------
    "EFF_SELECT_STAR": ("efficiency", WARNING, "SELECT * - project only the columns needed"),
    "EFF_NON_SARGABLE": ("efficiency", WARNING, "Function/CAST applied to a column in a predicate defeats index seeks"),
    "EFF_LEADING_WILDCARD": ("efficiency", WARNING, "LIKE with a leading wildcard cannot seek an index"),
    "EFF_CORRELATED_SCALAR": ("efficiency", WARNING, "Scalar subquery in the select list runs per row - prefer a join or CROSS/OUTER APPLY"),
    "EFF_CURSOR": ("efficiency", WARNING, "Cursor / WHILE loop - prefer a set-based rewrite"),
    "EFF_DISTINCT_UNION": ("efficiency", WARNING, "DISTINCT inside a UNION branch is redundant (UNION already dedupes)"),
    # --- Column existence checks (only with --metadata) -----------------
    "COLUMN_NOT_FOUND": ("column-check", ERROR, "Column not found in table/view metadata"),
    "ALIAS_COLUMN_NOT_FOUND": ("column-check", ERROR, "alias.column must name a column its #temp table, CTE or derived table defines"),
}

SYSTEM_SCHEMAS = {"sys", "information_schema", "tempdb", "master", "msdb"}

# sqlglot maps T-SQL `text` and `ntext` to TEXT, and `image` to IMAGE
LEGACY_TYPES = {exp.DataType.Type.TEXT, exp.DataType.Type.IMAGE}

# One or two letters, optionally numbered: x, V, p, ab, t1, x12
TERSE_ALIAS_RE = re.compile(r"^[A-Za-z]{1,2}\d*$")
# A stem plus a trailing number: FAM1, FAM_2
NUMBERED_ALIAS_RE = re.compile(r"^(.*?[A-Za-z])_?\d+$")

NONDETERMINISTIC_FUNCS = {"NEWID", "GETDATE", "GETUTCDATE", "SYSDATETIME", "RAND", "CURRENT_TIMESTAMP"}


# ---------------------------------------------------------------------------
# the metadata corpus catalogue
# ---------------------------------------------------------------------------

# Items in a CREATE TABLE body that are table constraints, not columns
TABLE_CONSTRAINT_STARTS = {
    "constraint", "primary", "unique", "foreign", "check", "index", "key",
    "period", "exclude",
}

METADATA_HEADER_RE = re.compile(r"^--\s*Object:\s*(.+?)\s*$", re.MULTILINE)
METADATA_SEPARATOR_RE = re.compile(r"^=+\s*$", re.MULTILINE)
IDENTIFIER_RE = re.compile(r'\[([^\]]+)\]|"([^"]+)"|([A-Za-z_@#][\w$@#]*)')


@dataclass
class MetadataObject:
    """One TABLE or VIEW as recorded in the metadata corpus."""
    key: str                      # lowercase database.schema.name
    database: str
    schema: str
    name: str
    obj_type: str                 # "TABLE" or "VIEW"
    columns: Dict[str, str] = field(default_factory=dict)  # lowercase -> as written
    complete: bool = True         # False when the column list could not be determined
    source: str = ""              # metadata file the object came from

    @property
    def qualified(self) -> str:
        return f"[{self.database}].[{self.schema}].[{self.name}]"

    def has(self, column: str) -> bool:
        return column.lower() in self.columns


class MetadataCatalog:
    """Column lists for every table and view under the metadata corpus.

    Objects are looked up by whatever name a query uses: three-part
    (db.schema.object), two-part (schema.object) or bare object name. Partial
    names can match several catalogued objects; a column is then accepted if
    any of the candidates defines it.
    """

    def __init__(self) -> None:
        self.objects: Dict[str, MetadataObject] = {}
        self._by_schema_name: Dict[str, List[MetadataObject]] = {}
        self._by_name: Dict[str, List[MetadataObject]] = {}
        self.unparsed: List[str] = []

    def __len__(self) -> int:
        return len(self.objects)

    @property
    def complete_objects(self) -> List[MetadataObject]:
        return [o for o in self.objects.values() if o.complete]

    def add(self, obj: MetadataObject) -> None:
        self.objects[obj.key] = obj
        self._by_schema_name.setdefault(f"{obj.schema}.{obj.name}", []).append(obj)
        self._by_name.setdefault(obj.name, []).append(obj)

    def lookup(self, parts: Tuple[str, ...]) -> List[MetadataObject]:
        """Candidate objects for a 1-, 2- or 3-part name (already lowercased)."""
        if len(parts) >= 3:
            obj = self.objects.get(".".join(parts[-3:]))
            return [obj] if obj else []
        if len(parts) == 2:
            return list(self._by_schema_name.get(".".join(parts), []))
        if len(parts) == 1:
            return list(self._by_name.get(parts[0], []))
        return []


def _skip_quoted(text: str, i: int) -> int:
    """Index just past the quoted/bracketed identifier or string starting at i."""
    ch = text[i]
    if ch == "[":
        end = text.find("]", i + 1)
        return len(text) if end < 0 else end + 1
    quote = ch
    i += 1
    while i < len(text):
        if text[i] == quote:
            if i + 1 < len(text) and text[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return i


def _balanced_span(text: str, start: int) -> Optional[Tuple[int, int]]:
    """Span inside the parentheses opening at text[start], ignoring quoted text."""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "['\"":
            i = _skip_quoted(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return start + 1, i
        i += 1
    return None


def _split_top_level(text: str) -> List[str]:
    """Split on commas that are not nested in parentheses or quoted identifiers."""
    items: List[str] = []
    start = depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "['\"":
            i = _skip_quoted(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
        i += 1
    items.append(text[start:])
    return items


def _leading_identifier(text: str) -> Optional[str]:
    m = IDENTIFIER_RE.match(text.strip())
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3)


def _parse_object_header(content: str, db_dir_name: str, obj_type: str,
                         file_path: Path) -> Optional[MetadataObject]:
    """Read the `-- Object: [db].[schema].[name]` header of a metadata file."""
    m = METADATA_HEADER_RE.search(content)
    if not m:
        return None
    parts = [a or b or c for a, b, c in IDENTIFIER_RE.findall(m.group(1))]
    if len(parts) < 2:
        return None
    if len(parts) == 2:
        parts.insert(0, db_dir_name)
    database, schema, name = (p.lower() for p in parts[-3:])
    return MetadataObject(
        key=f"{database}.{schema}.{name}",
        database=database,
        schema=schema,
        name=name,
        obj_type=obj_type,
        source=str(file_path),
    )


def _table_columns(content: str) -> Optional[List[str]]:
    """Column names from a CREATE TABLE definition, in declaration order."""
    m = re.search(r"CREATE\s+TABLE\b", content, re.IGNORECASE)
    if not m:
        return None
    open_paren = content.find("(", m.end())
    if open_paren < 0:
        return None
    span = _balanced_span(content, open_paren)
    if not span:
        return None
    columns: List[str] = []
    for item in _split_top_level(content[span[0]:span[1]]):
        name = _leading_identifier(item)
        if not name or name.lower() in TABLE_CONSTRAINT_STARTS:
            continue
        columns.append(name)
    return columns or None


def _unwrap_query(node: Optional[exp.Expression]) -> Optional[exp.Expression]:
    """Peel subqueries and set operations down to the first SELECT."""
    seen = 0
    while node is not None and seen < 50:
        seen += 1
        if isinstance(node, exp.Subquery):
            node = node.this
        elif isinstance(node, exp.SetOperation):
            node = node.this
        else:
            return node
    return node


def _view_columns(content: str) -> Optional[List[str]]:
    """Output column names of a CREATE VIEW, or None if they can't be determined."""
    body = content
    sep = METADATA_SEPARATOR_RE.search(content)
    if sep:
        body = content[sep.end():]
    try:
        tree = sqlglot.parse_one(body, dialect=DIALECT)
    except Exception:
        return None
    if tree is None:
        return None

    if isinstance(tree, exp.Create):
        # An explicit column list on the view wins: CREATE VIEW v (a, b) AS ...
        target = tree.this
        if isinstance(target, exp.Schema) and target.expressions:
            names = [e.name for e in target.expressions if e.name]
            if len(names) == len(target.expressions):
                return names
        query = tree.expression
    else:
        query = tree

    select = _unwrap_query(query)
    if not isinstance(select, exp.Select):
        return None
    if any(isinstance(strip_alias(e), exp.Star) for e in select.expressions):
        return None  # SELECT * - the real column list lives in the base tables
    names = select.named_selects
    if not names or not all(names):
        return None
    return names


def build_metadata_catalog(metadata_dir: Path = None) -> MetadataCatalog:
    """Index every TABLE and VIEW definition under the metadata corpus."""
    catalog = MetadataCatalog()
    root = metadata_dir
    if root is None or not root.exists():
        return catalog

    for db_dir in sorted(root.iterdir()):
        if not db_dir.is_dir():
            continue
        for obj_type in ("TABLE", "VIEW"):
            obj_dir = db_dir / obj_type
            if not obj_dir.is_dir():
                continue
            for f in sorted(obj_dir.glob("*.sql")):
                try:
                    content = f.read_text(errors="replace")
                except OSError:
                    continue
                obj = _parse_object_header(content, db_dir.name, obj_type, f)
                if obj is None:
                    catalog.unparsed.append(str(f))
                    continue
                columns = _table_columns(content) if obj_type == "TABLE" else _view_columns(content)
                if columns:
                    obj.columns = {c.lower(): c for c in columns}
                else:
                    # Keep the object (so the name is known) but never report
                    # missing columns against a list we could not read.
                    obj.complete = False
                    catalog.unparsed.append(str(f))
                catalog.add(obj)

    return catalog



# ---------------------------------------------------------------------------
# Column existence checking against the catalogue
# ---------------------------------------------------------------------------

SCOPE_TYPES = (exp.Select, exp.Update, exp.Delete, exp.Insert, exp.Merge)

# A source that exists but whose columns are unknowable (temp table, CTE,
# derived table, table-valued function, object missing from the catalogue).
UNKNOWN_SOURCE: List[MetadataObject] = []


def _table_name_parts(table: exp.Table) -> Tuple[str, ...]:
    return tuple(p.lower() for p in (table.catalog, table.db, table.name) if p)


def _resolve_table(table: exp.Table, catalog: MetadataCatalog) -> List[MetadataObject]:
    """Catalogued objects a table reference could mean ([] when unknowable)."""
    if is_temp_table(table) or table.name.startswith("@"):
        return UNKNOWN_SOURCE
    parts = _table_name_parts(table)
    if not parts or set(parts) & SYSTEM_SCHEMAS:
        return UNKNOWN_SOURCE
    candidates = catalog.lookup(parts)
    if not candidates or not all(o.complete for o in candidates):
        return UNKNOWN_SOURCE
    return candidates


def _shadowed_names(statement: exp.Expression) -> Set[str]:
    """Aliases that name something other than a catalogued object."""
    names: Set[str] = set()
    for cte in statement.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias.lower())
    for sub in statement.find_all(exp.Subquery):
        if sub.alias:
            names.add(sub.alias.lower())
    for func in statement.find_all(exp.Lateral, exp.Unnest):
        if func.alias:
            names.add(func.alias.lower())
    return names


def _scope_sources(scope: exp.Expression, catalog: MetadataCatalog,
                   shadowed: Set[str]) -> Dict[str, List[MetadataObject]]:
    """Names visible in this scope -> the objects they may refer to."""
    sources: Dict[str, List[MetadataObject]] = {}
    for table in local_find(scope, exp.Table):
        alias = (table.alias or table.name).lower()
        if not alias:
            continue
        if alias in shadowed or isinstance(table.this, exp.Func):
            sources[alias] = UNKNOWN_SOURCE
            continue
        sources[alias] = _resolve_table(table, catalog)
    return sources


def _enclosing_scopes(node: exp.Expression) -> List[exp.Expression]:
    """Scope nodes around `node`, innermost first."""
    scopes: List[exp.Expression] = []
    cur = node
    while cur is not None:
        if isinstance(cur, SCOPE_TYPES):
            scopes.append(cur)
        cur = cur.parent
    return scopes


PSEUDO_TABLES = {"inserted", "deleted"}   # OUTPUT / trigger pseudo-tables
SOURCE_TYPES = (exp.Table, exp.Subquery, exp.Lateral, exp.Values, exp.Pivot)


def _scope_source_nodes(scope: exp.Expression) -> Dict[str, exp.Expression]:
    """name -> node for every source a scope declares itself: FROM, JOIN, APPLY,
    the DML target. An alias wins over a bare table name; an INTO target and a
    PIVOT's own table are not readable names."""
    found: List[exp.Expression] = []

    def walk(node: exp.Expression) -> None:
        for child in node.iter_expressions():
            if isinstance(child, SOURCE_TYPES):
                found.append(child)
            if _is_new_scope(child):
                continue
            walk(child)

    walk(scope)
    names: Dict[str, exp.Expression] = {}
    for node in found:                               # bare names first ...
        if isinstance(node, exp.Table) and not node.alias and node.name \
                and not isinstance(node.parent, exp.Into):
            names[node.name.lower()] = node
    for node in found:                               # ... then aliases override
        if node.alias:
            names[node.alias.lower()] = node
    return names


def _projection_names(query: Optional[exp.Expression]) -> Optional[Set[str]]:
    """Output column names of a query (first branch of a set operation);
    None when unknowable (a *, an unnamed expression)."""
    while isinstance(query, (exp.Subquery, exp.SetOperation)):
        query = query.this
    if not isinstance(query, exp.Select):
        return None
    names: Set[str] = set()
    for proj in query.expressions:
        base = strip_alias(proj)
        if isinstance(base, exp.Star) or (isinstance(base, exp.Column) and isinstance(base.this, exp.Star)):
            return None
        if not proj.alias_or_name:
            return None
        names.add(proj.alias_or_name.lower())
    return names


def _alias_columns(node: exp.Expression) -> Optional[Set[str]]:
    """Columns an explicit alias list declares: (...) AS D(a, b), WITH C (a, b)."""
    alias = node.args.get("alias")
    cols = alias.args.get("columns") if isinstance(alias, exp.TableAlias) else None
    return {c.name.lower() for c in cols} if cols else None


def check_alias_columns(queries: List[Tuple[int, str]],
                        parsed: Dict[int, List[exp.Expression]]) -> List[Violation]:
    """ALIAS_COLUMN_NOT_FOUND: alias.column read from a #temp table, CTE or derived
    table whose columns this script defines (CREATE TABLE, SELECT ... INTO,
    ALTER TABLE ... ADD, a CTE or derived-table select list) must be one of them.
    A #temp table the script does not define (--assume-exists, dynamic SQL), or
    one defined with SELECT *, is not checked. Catalogued tables are
    COLUMN_NOT_FOUND's job."""
    temp_cols: Dict[str, Tuple[Optional[Set[str]], int]] = {}   # #name -> (columns, query)
    violations: List[Violation] = []
    reported: Set[Tuple[str, str]] = set()

    def temp_name(table: exp.Table) -> str:
        return "#" + table.name.lstrip("#").lower()

    for query_num, query_text in queries:
        for statement in parsed.get(query_num, []):
            cte_cols: Dict[str, Optional[Set[str]]] = {}
            for cte in statement.find_all(exp.CTE):
                if cte.alias:
                    cte_cols[cte.alias.lower()] = _alias_columns(cte) or _projection_names(cte.this)

            def known(node: exp.Expression) -> Tuple[str, Optional[Set[str]], str]:
                if isinstance(node, exp.Table):
                    if is_temp_table(node):
                        cols, made = temp_cols.get(temp_name(node), (None, 0))
                        return temp_name(node), cols, f"created in query #{made}"
                    if not node.db and node.name.lower() in cte_cols:
                        return f"CTE {node.name}", cte_cols[node.name.lower()], "its WITH clause"
                if isinstance(node, exp.Subquery):
                    cols = _alias_columns(node) or _projection_names(node.this)
                    return f"derived table {node.alias}", cols, "its select list"
                return "", None, ""

            def report(column_sql: str, name: str, label: str, cols: Set[str], where: str):
                if (label, name.lower()) in reported:
                    return
                reported.add((label, name.lower()))
                close = difflib.get_close_matches(name.lower(), sorted(cols), n=1, cutoff=0.8)
                hint = f" (did you mean '{close[0]}'?)" if close else ""
                violations.append(Violation(
                    rule="ALIAS_COLUMN_NOT_FOUND",
                    message=f"query #{query_num}: {column_sql}{hint} - {label} ({where}) "
                            f"has no column {name}; it has: {', '.join(sorted(cols))[:300]}"))

            sources_cache: Dict[int, Dict[str, exp.Expression]] = {}
            for column in statement.find_all(exp.Column):
                qualifier = column.table
                if not qualifier or column.db or not column.name or isinstance(column.this, exp.Star):
                    continue
                for scope in _enclosing_scopes(column):
                    if id(scope) not in sources_cache:
                        sources_cache[id(scope)] = _scope_source_nodes(scope)
                    node = sources_cache[id(scope)].get(qualifier.lower())
                    if node is None:
                        continue
                    label, cols, where = known(node)
                    if cols is not None and column.name.lower() not in cols:
                        report(sql_of(column), column.name, label, cols, where)
                    break

            for insert in statement.find_all(exp.Insert):
                target = insert.this
                if isinstance(target, exp.Schema) and isinstance(target.this, exp.Table) \
                        and is_temp_table(target.this):
                    label, cols, where = known(target.this)
                    for ident in target.expressions:
                        if cols is not None and ident.name and ident.name.lower() not in cols:
                            report(f"INSERT INTO {label} ({ident.name})", ident.name, label, cols, where)

            # register what this statement defines, after checking what it reads
            for create in statement.find_all(exp.Create):
                schema = create.this
                if isinstance(schema, exp.Schema) and isinstance(schema.this, exp.Table) \
                        and is_temp_table(schema.this):
                    temp_cols[temp_name(schema.this)] = (
                        {c.name.lower() for c in schema.expressions if isinstance(c, exp.ColumnDef)},
                        query_num)
            for select in statement.find_all(exp.Select):
                into = select.args.get("into")
                if into is not None and isinstance(into.this, exp.Table) and is_temp_table(into.this):
                    temp_cols[temp_name(into.this)] = (_projection_names(select), query_num)
            for alter in statement.find_all(exp.Alter):
                if isinstance(alter.this, exp.Table) and is_temp_table(alter.this):
                    name = temp_name(alter.this)
                    cols, made = temp_cols.get(name, (None, 0))
                    actions = alter.args.get("actions") or []
                    if cols is not None and all(isinstance(a, exp.ColumnDef) for a in actions):
                        temp_cols[name] = (cols | {a.name.lower() for a in actions}, made)
                    else:
                        temp_cols[name] = (None, made)

        # a create the parser did not see (unparsed statement, dynamic SQL) - stop checking it
        clean = strip_noise(query_text)
        for raw in CREATE_TEMP_RE.findall(clean) + INTO_TEMP_RE.findall(clean):
            name = "#" + raw.lstrip("#").lower()
            if temp_cols.get(name, (None, -1))[1] != query_num:
                temp_cols[name] = (None, query_num)

    return violations


def _missing_columns_in_statement(
    statement: exp.Expression,
    catalog: MetadataCatalog,
) -> List[Tuple[MetadataObject, str, int]]:
    """(object, column, candidate count) triples for columns absent from metadata.

    The candidate count is how many catalogued objects a partially qualified
    name could have meant; the column was missing from all of them.
    """
    shadowed = _shadowed_names(statement)
    sources_by_scope: Dict[int, Dict[str, List[MetadataObject]]] = {}

    def sources_of(scope: exp.Expression) -> Dict[str, List[MetadataObject]]:
        cached = sources_by_scope.get(id(scope))
        if cached is None:
            cached = _scope_sources(scope, catalog, shadowed)
            sources_by_scope[id(scope)] = cached
        return cached

    missing: List[Tuple[MetadataObject, str, int]] = []

    def record(candidates: List[MetadataObject], column: str) -> None:
        if not candidates or any(o.has(column) for o in candidates):
            return
        missing.append((candidates[0], column, len(candidates)))

    for column in statement.find_all(exp.Column):
        name = column.name
        if not name or name == "*" or isinstance(column.this, exp.Star):
            continue
        if name.startswith("@"):
            continue
        qualifier = column.table.lower()
        if qualifier:
            if qualifier in shadowed:
                continue
            for scope in _enclosing_scopes(column):
                sources = sources_of(scope)
                if qualifier in sources:
                    record(sources[qualifier], name)
                    break
            continue

        # Unqualified: only decidable when the innermost scope reads exactly
        # one source and that source is catalogued.
        scopes = _enclosing_scopes(column)
        if not scopes:
            continue
        sources = sources_of(scopes[0])
        if len(sources) != 1:
            continue
        record(next(iter(sources.values())), name)

    # Column lists on INSERT ... (col, col) name the target's columns
    for insert in statement.find_all(exp.Insert):
        target = insert.this
        if not isinstance(target, exp.Schema) or not isinstance(target.this, exp.Table):
            continue
        candidates = _resolve_table(target.this, catalog)
        for ident in target.expressions:
            if isinstance(ident, (exp.Identifier, exp.Column)) and ident.name:
                record(candidates, ident.name)

    return missing


def check_columns_against_metadata(
    parsed_by_query: Dict[int, List[exp.Expression]],
    catalog: MetadataCatalog,
) -> List[Violation]:
    """Every column read from a catalogued table or view must exist in its entry.

    Tables and views that the metadata corpus does not describe - temp tables,
    CTEs, derived tables, objects from other systems - are left alone; so are
    catalogued objects whose own column list could not be parsed.
    """
    violations: List[Violation] = []
    if not catalog.objects:
        return violations

    # object key -> lowercase column -> (column as written, first query number)
    missing_by_object: Dict[str, Dict[str, Tuple[str, int]]] = {}
    objects_by_key: Dict[str, MetadataObject] = {}
    candidates_by_key: Dict[str, int] = {}

    for query_num in sorted(parsed_by_query):
        for statement in parsed_by_query[query_num]:
            if statement is None:
                continue
            for obj, column, candidates in _missing_columns_in_statement(statement, catalog):
                objects_by_key[obj.key] = obj
                candidates_by_key[obj.key] = max(candidates_by_key.get(obj.key, 0), candidates)
                missing_by_object.setdefault(obj.key, {}).setdefault(
                    column.lower(), (column, query_num)
                )

    for key in sorted(missing_by_object):
        obj = objects_by_key[key]
        columns = missing_by_object[key]
        first_query = min(q for _, q in columns.values())
        detail = []
        for lowered in sorted(columns):
            written = columns[lowered][0]
            close = difflib.get_close_matches(lowered, obj.columns, n=1, cutoff=0.8)
            detail.append(
                f"{written} (did you mean '{obj.columns[close[0]]}'?)" if close else written
            )
        others = candidates_by_key[key] - 1
        also = f", nor in {others} other object(s) of the same name" if others > 0 else ""
        violations.append(
            Violation(
                rule="COLUMN_NOT_FOUND",
                message=f"{obj.qualified} ({obj.obj_type.lower()}, {len(obj.columns)} columns "
                        f"in metadata) has no column: {', '.join(detail)}{also} "
                        f"- first referenced in query #{first_query}",
            )
        )

    return violations


# ---------------------------------------------------------------------------
# Generic AST helpers
# ---------------------------------------------------------------------------

def sql_of(node: exp.Expression) -> str:
    try:
        return node.sql(dialect=DIALECT)
    except Exception:
        return str(node)


def norm(node: exp.Expression) -> str:
    """Normalized SQL text of an expression, for structural comparison."""
    return re.sub(r"\s+", " ", sql_of(node)).strip().upper()


def descendants(node: exp.Expression, prune=None) -> Iterable[exp.Expression]:
    """Yield descendant expressions, optionally pruning whole sub-trees."""
    for child in node.iter_expressions():
        if prune is not None and prune(child):
            continue
        yield child
        yield from descendants(child, prune)


def _is_new_scope(node: exp.Expression) -> bool:
    return isinstance(node, (exp.Subquery, exp.Select, exp.SetOperation))


def local(node: exp.Expression) -> Iterable[exp.Expression]:
    """Descendants within the current query scope (does not enter subqueries)."""
    return descendants(node, prune=_is_new_scope)


def local_find(node: exp.Expression, *types) -> List[exp.Expression]:
    """The node itself plus same-scope descendants matching the given types."""
    found = [n for n in local(node) if isinstance(n, types)]
    if isinstance(node, types):
        found.insert(0, node)
    return found


def grouping_aggregates(node: exp.Expression) -> List[exp.Expression]:
    """Aggregates that force a GROUP BY, i.e. aggregates outside any OVER clause."""
    if isinstance(node, exp.Window):
        return []
    prune = lambda n: _is_new_scope(n) or isinstance(n, exp.Window)
    out = [n for n in descendants(node, prune=prune) if isinstance(n, exp.AggFunc)]
    if isinstance(node, exp.AggFunc):
        out.insert(0, node)
    return out


def strip_alias(node: exp.Expression) -> exp.Expression:
    return node.this if isinstance(node, exp.Alias) else node


def is_temp_table(table: exp.Table) -> bool:
    ident = table.this
    if isinstance(ident, exp.Identifier):
        if ident.args.get("temporary") or ident.args.get("global_"):
            return True
    return sql_of(table).lstrip().startswith("#")


def table_key(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts).lower()


def literal_kind(node: exp.Expression) -> Optional[str]:
    """Best-effort static type class of an expression ('string' / 'numeric')."""
    node = strip_alias(node)
    if isinstance(node, exp.Literal):
        return "string" if node.is_string else "numeric"
    if isinstance(node, (exp.Cast, exp.TryCast)):
        to = node.to
        if isinstance(to, exp.DataType):
            if to.this in exp.DataType.TEXT_TYPES:
                return "string"
            if to.this in exp.DataType.NUMERIC_TYPES:
                return "numeric"
    if isinstance(node, exp.Boolean):
        return "numeric"
    return None


def in_new_scope_between(node: exp.Expression, ancestor: exp.Expression) -> bool:
    """True if a query-scope boundary sits between node and ancestor."""
    cur = node.parent
    while cur is not None and cur is not ancestor:
        if _is_new_scope(cur):
            return True
        cur = cur.parent
    return False


def func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).upper()
    return type(node).__name__.upper()


# ---------------------------------------------------------------------------
# Statement extraction
# ---------------------------------------------------------------------------

COMMENT_LINE_RE = re.compile(r"--[^\n]*")
COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_RE = re.compile(r"'(?:[^']|'')*'")


def strip_noise(text: str) -> str:
    """Remove comments and string literals so regex scans do not trip on them."""
    text = COMMENT_BLOCK_RE.sub(" ", text)
    text = COMMENT_LINE_RE.sub(" ", text)
    return STRING_RE.sub("''", text)


def _scan_line(line: str, state: Optional[tuple]) -> Tuple[Optional[tuple], bool]:
    """Lex one line, carrying comment/literal state across lines.

    state is None (code), ('block', depth) for a /* */ comment (T-SQL nests
    them), or (closer,) inside a '...', "..." or [...] that spans lines.
    Returns (state at end of line, whether the line's last code character is
    ';'). A ';' inside a comment or literal never ends a statement, and a
    trailing -- comment after a real ';' does not hide it.
    """
    i, n = 0, len(line)
    last_code = ""
    while i < n:
        ch = line[i]
        if state is not None and state[0] == "block":
            if line.startswith("*/", i):
                depth = state[1] - 1
                state = None if depth == 0 else ("block", depth)
                i += 2
            elif line.startswith("/*", i):
                state = ("block", state[1] + 1)
                i += 2
            else:
                i += 1
            continue
        if state is not None:
            closer = state[0]
            if ch == closer:
                if i + 1 < n and line[i + 1] == closer:  # '' "" ]] escapes
                    i += 2
                    continue
                state = None
                last_code = ch
            i += 1
            continue
        if line.startswith("--", i):
            break
        if line.startswith("/*", i):
            state = ("block", 1)
            i += 2
            continue
        if ch in "'\"":
            state = (ch,)
        elif ch == "[":
            state = ("]",)
        if not ch.isspace():
            last_code = ch
        i += 1
    return state, state is None and last_code == ";"


GO_RE = re.compile(r"^\s*GO(\s+\d+)?\s*(--.*)?$", re.IGNORECASE)


def extract_queries_from_file(file_path: str) -> List[tuple]:
    """Extract individual queries from a SQL file, splitting on semicolons.

    Only a ';' ending a line in code splits; one inside a comment or string
    literal (common in commented-out SQL and dynamic-SQL batches) does not.
    """
    content = None
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if content is None:
        with open(file_path, 'r', errors='replace') as f:
            content = f.read()

    queries = []
    lines = content.split('\n')
    current_query_lines = []
    query_num = 0
    state = None

    for line in lines:
        stripped = line.strip()
        if stripped == '':
            if current_query_lines:
                current_query_lines.append(line)
            continue

        if state is None and GO_RE.match(line):
            # a batch separator ends the statement before it and is not SQL:
            # left in, "GO\n<stmt>" parses as an opaque Command and goes unchecked
            query_text = '\n'.join(current_query_lines).strip()
            if strip_noise(query_text).strip():
                query_num += 1
                queries.append((query_num, query_text))
                current_query_lines = []
            continue

        current_query_lines.append(line)
        state, ends_statement = _scan_line(line, state)

        if ends_statement:
            query_text = '\n'.join(current_query_lines).strip()
            if query_text:
                query_num += 1
                queries.append((query_num, query_text))
            current_query_lines = []

    # Handle last query without semicolon (a comment-only tail is not a query)
    if current_query_lines:
        query_text = '\n'.join(current_query_lines).strip()
        if strip_noise(query_text).strip():
            query_num += 1
            queries.append((query_num, query_text))

    return queries


def detect_missing_select_keyword(query_text: str) -> Optional[str]:
    """Detect the common pattern of missing SELECT keyword in subqueries.

    Common bug: FROM (\\n    CASE ... END AS x, ... FROM t)
    Should be: FROM (\\n    SELECT\\n        CASE ... END AS x, ... FROM t)
    """
    lines = query_text.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.upper().startswith('FROM ('):
            for j in range(i + 1, min(i + 5, len(lines))):
                next_line = lines[j].strip()
                if next_line:
                    if next_line.upper().startswith('CASE'):
                        return (
                            f"Line ~{i+1}: Missing 'SELECT' keyword after 'FROM ('. "
                            f"The subquery starts with 'CASE' directly. "
                            f"Should be: FROM (\\n    SELECT\\n        CASE ..."
                        )
                    elif next_line.upper().startswith('SELECT'):
                        break
                    else:
                        break
    return None


def check_query_features(query_text: str) -> List[str]:
    """Check for features in the query that may not be available in SQL Server 2016.

    Features in ALLOWED_POST_2016_FEATURES are assumed available.
    Only features in POST_SQLSERVER_2016_FEATURES are flagged as errors.
    """
    issues = []
    upper_query = strip_noise(query_text).upper()

    for feature, description in POST_SQLSERVER_2016_FEATURES.items():
        if re.search(r'\b' + re.escape(feature) + r'\b', upper_query):
            issues.append(description)

    return issues


def analyze_expression_tree(node: exp.Expression, features: List[str], warnings: List[str]):
    """Recursively analyze the AST for features."""
    if isinstance(node, exp.GroupConcat):
        features.append("STRING_AGG / GROUP_CONCAT (check version compatibility)")
    elif isinstance(node, exp.Window):
        warnings.append("Window functions (OVER clause) - SQL Server 2012+")
    elif isinstance(node, exp.Lateral):
        features.append("CROSS APPLY / OUTER APPLY (LATERAL) - SQL Server 2005+")
    elif isinstance(node, exp.With):
        features.append("CTE (Common Table Expression) - SQL Server 2005+")
    elif isinstance(node, exp.Pivot):
        features.append("PIVOT - SQL Server 2005+")
    elif isinstance(node, exp.Merge):
        features.append("MERGE - SQL Server 2008+")
    elif isinstance(node, exp.TryCast):
        features.append("TRY_CAST - SQL Server 2012+")
    elif isinstance(node, exp.JSON):
        features.append("JSON functions - SQL Server 2016+")
    elif isinstance(node, exp.ArrayAgg):
        features.append("ARRAY_AGG (check version compatibility)")
    elif isinstance(node, exp.WithinGroup):
        features.append("WITHIN GROUP - SQL Server 2017+")
    elif isinstance(node, exp.WindowSpec):
        warnings.append("Window frame specification - SQL Server 2012+")
    elif isinstance(node, exp.Offset):
        features.append("OFFSET/FETCH - SQL Server 2012+")
    elif isinstance(node, exp.Fetch):
        features.append("FETCH FIRST/NEXT - SQL Server 2012+")
    elif isinstance(node, exp.ForClause):
        kind = node.args.get("kind")
        kind_name = kind.name if isinstance(kind, exp.Expression) else str(kind or "")
        if kind_name.upper() == 'XML':
            features.append("FOR XML PATH - SQL Server 2000+")

    for child in node.iter_expressions():
        analyze_expression_tree(child, features, warnings)


# ---------------------------------------------------------------------------
# T-SQL constraint checks (README "author checklist") + house style
# ---------------------------------------------------------------------------

class Checker:
    """Collects rule violations for a single parsed statement."""

    def __init__(self, statement: exp.Expression, query_text: str, optimise: bool):
        self.statement = statement
        self.query_text = query_text
        self.clean_text = strip_noise(query_text)
        self.optimise = optimise
        self.violations: List[Violation] = []

    def add(self, rule: str, message: str, severity: Optional[str] = None):
        category, default_severity, _ = RULES[rule]
        if category == "decomposition" and self.optimise:
            return
        if category == "efficiency" and not self.optimise:
            return
        self.violations.append(
            Violation(rule=rule, message=message, severity=severity or default_severity)
        )

    # -- entry point --------------------------------------------------------
    def run(self) -> List[Violation]:
        for select in self._nodes(exp.Select):
            self.check_select(select)
        for scope in self._nodes(exp.Select, exp.Update, exp.Delete):
            self.check_unknown_aliases(scope)
        for setop in self._nodes(exp.SetOperation):
            self.check_set_operation(setop)
        for with_ in self._nodes(exp.With):
            self.check_with(with_)
        for case in self._nodes(exp.Case):
            self.check_case(case)
        for coalesce in self._nodes(exp.Coalesce):
            self.check_coalesce(coalesce)
        for subquery in self._nodes(exp.Subquery):
            self.check_scalar_subquery(subquery)
        for not_node in self._nodes(exp.Not):
            self.check_not_in(not_node)
        for agg in self._nodes(exp.GroupConcat):
            self.check_string_agg(agg)
        for dtype in self._nodes(exp.DataType):
            self.check_legacy_type(dtype)

        self.check_xml_aggregation()
        self.check_aliases()
        if self.optimise:
            self.check_efficiency()
        else:
            self.check_decomposition()
        return self.violations

    def _nodes(self, *types) -> List[exp.Expression]:
        stmt = self.statement
        out = [n for n in descendants(stmt) if isinstance(n, types)]
        if isinstance(stmt, types):
            out.insert(0, stmt)
        return out

    # -- SELECT-scoped checks ----------------------------------------------
    def check_select(self, select: exp.Select):
        projections = list(select.expressions)
        group = select.args.get("group")
        where = select.args.get("where")
        having = select.args.get("having")
        order = select.args.get("order")

        alias_map = {
            e.alias.lower(): strip_alias(e)
            for e in projections
            if isinstance(e, exp.Alias) and e.alias
        }

        group_set: Set[str] = set()
        if group is not None:
            group_set = self.check_group_clause(group, alias_map)

        self.check_projection_grouping(select, projections, group, group_set)
        self.check_nested_aggregates(select)
        self.check_nested_windows(select)

        if where is not None:
            for agg in grouping_aggregates(where):
                self.add("AGG_IN_WHERE",
                         f"aggregate in WHERE clause: {sql_of(agg)} (move it to HAVING)")
            for win in [n for n in local(where) if isinstance(n, exp.Window)]:
                self.add("WINDOW_IN_WHERE",
                         f"window function in WHERE clause: {sql_of(win)} "
                         f"(materialize it first, then filter)")

        if having is not None:
            self.check_having(having, group_set, alias_map)

        if order is not None:
            self.check_order(select, order, projections, alias_map)

        self.check_distinct_legacy_types(select)
        self.check_ambiguous_columns(select)

    def check_group_clause(self, group: exp.Group,
                           alias_map: Dict[str, exp.Expression]) -> Set[str]:
        group_set: Set[str] = set()
        for gexpr in group.expressions:
            if isinstance(gexpr, exp.Ordered):
                gexpr = gexpr.this

            if grouping_aggregates(gexpr):
                self.add("GROUP_BY_AGGREGATE",
                         f"GROUP BY expression contains an aggregate: {sql_of(gexpr)}")
            if any(isinstance(n, (exp.Subquery, exp.Select)) for n in descendants(gexpr)):
                self.add("GROUP_BY_SUBQUERY",
                         f"GROUP BY expression contains a subquery: {sql_of(gexpr)}")
            if local_find(gexpr, exp.Window):
                self.add("GROUP_BY_WINDOW",
                         f"GROUP BY expression contains a window function: {sql_of(gexpr)}")

            for func in local_find(gexpr, exp.Func):
                if func_name(func) in NONDETERMINISTIC_FUNCS:
                    self.add("GROUP_BY_NONDETERMINISTIC",
                             f"non-deterministic expression in GROUP BY: {sql_of(gexpr)}")

            if isinstance(gexpr, exp.Column) and not gexpr.table:
                name = gexpr.name.lower()
                aliased = alias_map.get(name)
                if aliased is not None and not (
                    isinstance(aliased, exp.Column) and aliased.name.lower() == name
                ):
                    self.add("GROUP_BY_ALIAS",
                             f"GROUP BY references the SELECT alias '{gexpr.name}'; "
                             f"repeat the expression ({sql_of(aliased)[:60]}) or precompute it")
                    # keep the aliased expression covered so we do not also report
                    # GROUP_BY_MISSING_COLUMN for every column inside it
                    group_set.add(norm(aliased))
                    for col in local_find(aliased, exp.Column):
                        group_set.add(norm(col))
                        group_set.add(col.name.upper())

            group_set.add(norm(gexpr))
            if isinstance(gexpr, exp.Column):
                group_set.add(gexpr.name.upper())
        return group_set

    def _uncovered_columns(self, node: exp.Expression, group_set: Set[str]) -> List[exp.Column]:
        """Columns in `node` that are neither aggregated nor covered by GROUP BY."""
        out: List[exp.Column] = []

        def rec(n: exp.Expression):
            if isinstance(n, exp.AggFunc):
                return
            if isinstance(n, (exp.Subquery, exp.Select, exp.SetOperation)):
                return  # correlated references are resolved in the inner scope
            if norm(n) in group_set:
                return
            if isinstance(n, exp.Window) and not group_set:
                return  # window functions do not themselves require a GROUP BY
            if isinstance(n, exp.Column):
                if n.name.upper() in group_set:
                    return
                out.append(n)
                return
            for child in n.iter_expressions():
                rec(child)

        rec(strip_alias(node))
        return out

    def check_projection_grouping(self, select: exp.Select, projections: List[exp.Expression],
                                  group: Optional[exp.Group], group_set: Set[str]):
        has_agg = any(grouping_aggregates(p) for p in projections)
        if group is None and not has_agg:
            return
        if select.args.get("from_") is None and group is None:
            return

        for proj in projections:
            if isinstance(strip_alias(proj), exp.Star):
                self.add("GROUP_BY_MISSING_COLUMN",
                         "SELECT * cannot be combined with GROUP BY / aggregates")
                continue
            uncovered = self._uncovered_columns(proj, group_set)
            if not uncovered:
                continue
            names = ", ".join(sorted({sql_of(c) for c in uncovered}))
            if group is None:
                self.add("MIXED_AGG_DETAIL",
                         f"column(s) {names} are selected alongside aggregates with no GROUP BY")
            else:
                self.add("GROUP_BY_MISSING_COLUMN",
                         f"column(s) {names} in the select list are neither aggregated "
                         f"nor present in GROUP BY")

    def check_nested_aggregates(self, select: exp.Select):
        for agg in grouping_aggregates(select):
            if any(isinstance(inner, exp.AggFunc) for inner in local(agg)):
                self.add("NESTED_AGGREGATE",
                         f"aggregate nested inside another aggregate: {sql_of(agg)[:80]}")
            if select.args.get("from_") is None:
                self.add("AGG_OUT_OF_SCOPE",
                         f"aggregate {sql_of(agg)[:60]} used in a scope with no FROM clause")

    def check_nested_windows(self, select: exp.Select):
        for win in [n for n in local(select) if isinstance(n, exp.Window)]:
            if any(isinstance(inner, exp.Window) for inner in local(win)):
                self.add("NESTED_WINDOW",
                         f"window function nested inside another window function: "
                         f"{sql_of(win)[:80]}")

    def check_having(self, having: exp.Having, group_set: Set[str],
                     alias_map: Dict[str, exp.Expression]):
        uncovered = self._uncovered_columns(having.this, group_set)
        if uncovered:
            names = ", ".join(sorted({sql_of(c) for c in uncovered}))
            self.add("HAVING_INVALID",
                     f"HAVING references {names}, which is neither grouped nor aggregated")
        for col in [n for n in local(having) if isinstance(n, exp.Column)]:
            if not col.table and col.name.lower() in alias_map:
                self.add("HAVING_INVALID",
                         f"HAVING references the SELECT alias '{col.name}'")

    def check_order(self, select: exp.Select, order: exp.Order,
                    projections: List[exp.Expression], alias_map: Dict[str, exp.Expression]):
        has_companion = any(
            select.args.get(k) is not None for k in ("limit", "offset", "for_")
        )
        if self._is_nested_scope(select) and not has_companion:
            self.add("NESTED_ORDER_BY",
                     "ORDER BY inside a derived table/CTE/subquery requires TOP, "
                     "OFFSET ... FETCH or FOR XML")

        if select.args.get("distinct") is not None:
            select_set = {norm(strip_alias(p)) for p in projections}
            select_set |= {a.upper() for a in alias_map}
            for ordered in order.expressions:
                target = ordered.this if isinstance(ordered, exp.Ordered) else ordered
                if isinstance(target, exp.Column) and not target.table \
                        and target.name.upper() in select_set:
                    continue
                if norm(target) not in select_set:
                    self.add("DISTINCT_ORDER_BY",
                             f"ORDER BY {sql_of(target)} is not in the SELECT DISTINCT list")

    def _is_nested_scope(self, select: exp.Select) -> bool:
        cur = select.parent
        while cur is not None:
            if isinstance(cur, (exp.Subquery, exp.CTE)):
                return True
            if isinstance(cur, exp.Create) and str(cur.args.get("kind") or "").upper() == "VIEW":
                return True
            cur = cur.parent
        return False

    def check_distinct_legacy_types(self, select: exp.Select):
        group = select.args.get("group")
        distinct = select.args.get("distinct")
        if group is None and distinct is None:
            return
        targets: List[exp.Expression] = list(select.expressions)
        if group is not None:
            targets += list(group.expressions)
        reported: Set[str] = set()
        for target in targets:
            for dtype in local_find(target, exp.DataType):
                if dtype.this not in LEGACY_TYPES:
                    continue
                key = norm(strip_alias(target))
                if key in reported:
                    continue
                reported.add(key)
                self.add("LEGACY_TYPE_GROUPED",
                         f"a text/ntext/image value is grouped or de-duplicated: "
                         f"{sql_of(strip_alias(target))[:60]} "
                         f"(CAST to varchar(max)/nvarchar(max) first)")

    def check_ambiguous_columns(self, select: exp.Select):
        sources: List[exp.Expression] = []
        from_ = select.args.get("from_")
        if from_ is not None and isinstance(from_.this, (exp.Table, exp.Subquery)):
            sources.append(from_.this)
        for join in select.args.get("joins") or []:
            if isinstance(join.this, (exp.Table, exp.Subquery, exp.Lateral)):
                sources.append(join.this)
        if len(sources) < 2:
            return

        bare: Set[str] = set()
        for col in [n for n in local(select) if isinstance(n, exp.Column)]:
            if in_new_scope_between(col, select):
                continue
            if not col.table:
                bare.add(col.name)
        if bare:
            self.add("AMBIGUOUS_COLUMN",
                     f"unqualified column(s) with {len(sources)} sources in scope: "
                     f"{', '.join(sorted(bare))}")

    def check_unknown_aliases(self, scope: exp.Expression):
        """Every alias.column must name a source declared in this scope or an
        enclosing one (correlated subqueries), whatever the number of sources."""
        visible = set(PSEUDO_TABLES)
        cur = scope
        while cur is not None:
            if isinstance(cur, SCOPE_TYPES):
                visible |= set(_scope_source_nodes(cur))
            cur = cur.parent
        unknown = {
            sql_of(col) for col in local_find(scope, exp.Column)
            if col.table and not col.db and not col.table.startswith("@")
            and col.table.lower() not in visible
        }
        if unknown:
            self.add("UNKNOWN_ALIAS",
                     f"column(s) qualified by an alias that no FROM/JOIN/APPLY declares: "
                     f"{', '.join(sorted(unknown))}")

    # -- style ----------------------------------------------------------------
    def check_aliases(self):
        """Flag terse table aliases (x, V, t1, ab2) and numbered copies of one
        alias stem (FAM1/FAM2) - SQL-STYLE.md wants a name or a role."""
        aliases: List[str] = []
        for source in self._nodes(exp.Table, exp.Subquery):
            alias = source.alias
            if alias and alias.lower() != (getattr(source, "name", "") or "").lower():
                aliases.append(alias)
        terse = sorted({a for a in aliases if TERSE_ALIAS_RE.match(a)}, key=str.lower)
        if terse:
            self.add("ALIAS_NOT_DESCRIPTIVE",
                     f"terse table alias(es): {', '.join(terse)} - name the table or "
                     f"its role (e.g. CUST_BILLING, ORDER_PARENT)")
        stems: Dict[str, Set[str]] = {}
        for a in aliases:
            m = NUMBERED_ALIAS_RE.match(a)
            if m and a not in terse:
                stems.setdefault(m.group(1).lower(), set()).add(a)
        for group in stems.values():
            if len(group) > 1:
                self.add("ALIAS_NOT_DESCRIPTIVE",
                         f"numbered copies of one alias: {', '.join(sorted(group))} - "
                         f"suffix each with its role instead (e.g. CUST_BILLING / CUST_SHIPPING)")

    # -- subqueries ---------------------------------------------------------
    def check_scalar_subquery(self, subquery: exp.Subquery):
        parent = subquery.parent
        if parent is None or isinstance(parent, (exp.From, exp.Join, exp.Lateral,
                                                 exp.With, exp.CTE)):
            return
        if isinstance(parent, exp.In) and parent.args.get("query") is subquery:
            return  # IN (SELECT ...) is a row-set context, not a scalar one
        if isinstance(parent, exp.Exists):
            return
        inner = subquery.this
        if not isinstance(inner, exp.Select):
            return

        projections = list(inner.expressions)
        if len(projections) > 1:
            self.add("SCALAR_SUBQUERY_COLUMNS",
                     f"scalar subquery returns {len(projections)} columns: "
                     f"{sql_of(subquery)[:80]}")
        has_agg = any(grouping_aggregates(p) for p in projections)
        if not has_agg and inner.args.get("limit") is None:
            self.add("SCALAR_SUBQUERY_ROWS",
                     f"scalar subquery has no aggregate or TOP 1 and may return more "
                     f"than one row: {sql_of(subquery)[:80]}")

    def check_not_in(self, node: exp.Not):
        inner = node.this
        if isinstance(inner, exp.In) and (
            inner.args.get("query") is not None
            or isinstance(inner.args.get("field"), (exp.Select, exp.Subquery))
        ):
            self.add("NOT_IN_NULL",
                     f"NOT IN over a subquery ({sql_of(node)[:60]}): a NULL in the subquery "
                     f"result makes this match nothing - use a LEFT JOIN ... IS NULL anti-join")

    # -- set operations and CTEs -------------------------------------------
    def _branch_projections(self, node: exp.Expression) -> Optional[List[exp.Expression]]:
        if isinstance(node, exp.Subquery):
            return self._branch_projections(node.this)
        if isinstance(node, exp.SetOperation):
            return self._branch_projections(node.this)
        if isinstance(node, exp.Select):
            if any(isinstance(strip_alias(e), exp.Star) for e in node.expressions):
                return None
            return list(node.expressions)
        return None

    def check_set_operation(self, setop: exp.SetOperation):
        left = self._branch_projections(setop.this)
        right = self._branch_projections(setop.expression)
        if left is None or right is None:
            return
        kind = type(setop).__name__.upper()
        if len(left) != len(right):
            self.add("UNION_COLUMN_COUNT",
                     f"{kind} branches return {len(left)} and {len(right)} columns")
            return
        for i, (l, r) in enumerate(zip(left, right), start=1):
            lk, rk = literal_kind(l), literal_kind(r)
            if lk and rk and lk != rk:
                self.add("UNION_TYPE_COMPAT",
                         f"{kind} column {i}: {lk} ({sql_of(l)[:30]}) vs "
                         f"{rk} ({sql_of(r)[:30]})")

    def check_with(self, with_: exp.With):
        for cte in with_.expressions:
            name = cte.alias_or_name
            if not name:
                continue
            body = cte.this
            self_refs = [t for t in body.find_all(exp.Table) if t.name.lower() == name.lower()]
            if not self_refs:
                continue  # not recursive
            if not isinstance(body, exp.SetOperation):
                self.add("RECURSIVE_CTE_MEMBERS",
                         f"recursive CTE '{name}' references itself but has no "
                         f"anchor / recursive UNION ALL pair")
                continue
            if body.args.get("distinct"):
                self.add("RECURSIVE_CTE_DISALLOWED",
                         f"recursive CTE '{name}' must use UNION ALL, not UNION")

            recursive_member = body.expression
            if not isinstance(recursive_member, exp.Select):
                continue
            bad: List[str] = []
            if recursive_member.args.get("distinct") is not None:
                bad.append("DISTINCT")
            if recursive_member.args.get("group") is not None:
                bad.append("GROUP BY")
            if recursive_member.args.get("having") is not None:
                bad.append("HAVING")
            if recursive_member.args.get("order") is not None:
                bad.append("ORDER BY")
            if recursive_member.args.get("limit") is not None:
                bad.append("TOP")
            if any(isinstance(n, exp.AggFunc) for n in local(recursive_member)):
                bad.append("aggregate functions")
            if any(isinstance(n, exp.Subquery) for n in descendants(recursive_member)):
                bad.append("subqueries")
            for join in recursive_member.args.get("joins") or []:
                side = str(join.side or "").upper()
                if side in {"LEFT", "RIGHT", "FULL"}:
                    bad.append(f"{side} OUTER JOIN")
            if bad:
                self.add("RECURSIVE_CTE_DISALLOWED",
                         f"recursive member of CTE '{name}' contains: {', '.join(bad)}")

    # -- data type compatibility -------------------------------------------
    def check_case(self, case: exp.Case):
        results = [i.args.get("true") for i in case.args.get("ifs") or []]
        if case.args.get("default") is not None:
            results.append(case.args.get("default"))
        kinds = {literal_kind(r) for r in results if r is not None}
        kinds.discard(None)
        if len(kinds) > 1:
            self.add("CASE_TYPE_COMPAT",
                     f"CASE mixes {', '.join(sorted(kinds))} results: {sql_of(case)[:80]}")

    def check_coalesce(self, coalesce: exp.Coalesce):
        args = [coalesce.this] + list(coalesce.expressions or [])
        kinds = {literal_kind(a) for a in args if a is not None}
        kinds.discard(None)
        if len(kinds) > 1:
            name = "ISNULL" if coalesce.args.get("is_null") else "COALESCE"
            self.add("COALESCE_TYPE_COMPAT",
                     f"{name} mixes {', '.join(sorted(kinds))} arguments: "
                     f"{sql_of(coalesce)[:80]}")

    def check_legacy_type(self, dtype: exp.DataType):
        if dtype.this in LEGACY_TYPES:
            self.add("LEGACY_TYPE",
                     f"legacy {dtype.this.name.lower()} type used; prefer "
                     f"varchar(max) / nvarchar(max) / varbinary(max)")

    # -- ordering determinism / XML ----------------------------------------
    def check_string_agg(self, agg: exp.GroupConcat):
        if not isinstance(agg.this, exp.Order):
            self.add("ORDER_NOT_GUARANTEED",
                     f"STRING_AGG without WITHIN GROUP (ORDER BY ...): {sql_of(agg)[:80]}")

    def check_xml_aggregation(self):
        text = self.clean_text.upper()
        if "FOR XML PATH" not in text:
            return
        if ".VALUE(" not in text or "TYPE" not in text:
            self.add("XML_ENCODING",
                     "FOR XML PATH aggregation without \", TYPE).value('.', 'nvarchar(max)')\" "
                     "XML-encodes &, < and > and truncates at 8000 bytes")

    # -- house-style decomposition (default mode) --------------------------
    def check_decomposition(self):
        for with_ in self._nodes(exp.With):
            names = ", ".join(cte.alias_or_name for cte in with_.expressions)
            self.add("DECOMP_CTE",
                     f"WITH CTE ({names}) - materialize it into a #temp table instead")

        for subquery in self._nodes(exp.Subquery):
            parent = subquery.parent
            if isinstance(parent, (exp.CTE, exp.With)):
                continue
            if isinstance(parent, (exp.From, exp.Join, exp.Lateral)):
                where = "JOIN" if isinstance(parent, exp.Join) else "FROM"
                self.add("DECOMP_DERIVED_TABLE",
                         f"derived table in {where}: {sql_of(subquery)[:80]}")
            elif isinstance(parent, (exp.In, exp.Exists)):
                continue  # reported by the IN / EXISTS checks below
            else:
                self.add("DECOMP_SUBQUERY",
                         f"inner query: {sql_of(subquery)[:80]}")

        for node in self._nodes(exp.Exists):
            self.add("DECOMP_SUBQUERY",
                     f"EXISTS (SELECT ...): {sql_of(node)[:60]} - use an INNER JOIN against a "
                     f"distinct #temp table (LEFT JOIN ... IS NULL for NOT EXISTS)")

        for node in self._nodes(exp.In):
            if node.args.get("query") is not None or isinstance(
                node.args.get("field"), (exp.Select, exp.Subquery)
            ):
                self.add("DECOMP_SUBQUERY",
                         f"IN (SELECT ...): {sql_of(node)[:60]} - join to a #temp table instead")

        # repeated expressions: the same non-trivial expression in SELECT and GROUP BY
        for select in self._nodes(exp.Select):
            group = select.args.get("group")
            if group is None:
                continue
            group_norms = {norm(g) for g in group.expressions}
            for proj in select.expressions:
                base = strip_alias(proj)
                if isinstance(base, (exp.Column, exp.Literal, exp.Star)):
                    continue
                if norm(base) in group_norms:
                    self.add("DECOMP_REPEATED_EXPR",
                             f"{sql_of(base)[:60]} is computed in both the SELECT list and "
                             f"GROUP BY - compute it once into a #temp table column")

    # -- efficiency advisories (default mode) ------------------------------
    def check_efficiency(self):
        for select in self._nodes(exp.Select):
            if any(isinstance(strip_alias(e), exp.Star) for e in select.expressions):
                self.add("EFF_SELECT_STAR", "SELECT * - project only the columns needed")
            parent = select.parent
            if select.args.get("distinct") is not None and isinstance(parent, exp.Union) \
                    and parent.args.get("distinct"):
                self.add("EFF_DISTINCT_UNION",
                         "DISTINCT inside a UNION branch (UNION already removes duplicates)")
            where = select.args.get("where")
            if where is not None:
                self._check_sargability(where)
            for join in select.args.get("joins") or []:
                if join.args.get("on") is not None:
                    self._check_sargability(join.args["on"])

        for subquery in self._nodes(exp.Subquery):
            parent = subquery.parent
            if isinstance(parent, (exp.From, exp.Join, exp.Lateral, exp.With, exp.CTE)):
                continue
            if isinstance(subquery.this, exp.Select) and isinstance(parent, (exp.Select, exp.Alias)):
                self.add("EFF_CORRELATED_SCALAR",
                         f"scalar subquery in the select list: {sql_of(subquery)[:80]}")

        text = self.clean_text.upper()
        if re.search(r"\bDECLARE\s+\S+\s+CURSOR\b", text) or re.search(r"\bWHILE\b", text):
            self.add("EFF_CURSOR",
                     "cursor or WHILE loop detected - prefer a single set-based statement")

    def _check_sargability(self, node: exp.Expression):
        for pred in local_find(node, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like):
            if isinstance(pred, exp.Like):
                right = pred.expression
                if isinstance(right, exp.Literal) and right.is_string \
                        and right.this.startswith('%'):
                    self.add("EFF_LEADING_WILDCARD",
                             f"leading-wildcard LIKE: {sql_of(pred)[:80]}")
            left = pred.this
            if isinstance(left, (exp.Cast, exp.Func)) and local_find(left, exp.Column):
                self.add("EFF_NON_SARGABLE",
                         f"function applied to a column in a predicate: {sql_of(pred)[:80]}")


# ---------------------------------------------------------------------------
# Script-level checks (temp table lifecycle, base table scans)
# ---------------------------------------------------------------------------

TEMP_NAME_RE = re.compile(r"(##?\w+)")
CREATE_TEMP_RE = re.compile(r"\bCREATE\s+TABLE\s+(##?\w+)", re.IGNORECASE)
INTO_TEMP_RE = re.compile(r"\bINTO\s+(##?\w+)", re.IGNORECASE)
DROP_TEMP_RE = re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([^;]+)", re.IGNORECASE)
DROP_STATEMENT_RE = re.compile(
    r"(?:IF\s+OBJECT_ID\s*\([^)]*\)\s+IS\s+NOT\s+NULL\s+)?"
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[^;]+;?",
    re.IGNORECASE,
)


def is_drop_only(statement_text: str) -> bool:
    """True if the statement does nothing but drop tables."""
    remainder = DROP_STATEMENT_RE.sub(" ", statement_text)
    return remainder.strip(" \t\r\n;") == ""


def _drop_targets(statement_text: str) -> List[str]:
    names: List[str] = []
    for match in DROP_TEMP_RE.finditer(statement_text):
        names.extend(name.lower() for name in TEMP_NAME_RE.findall(match.group(1)))
    return names


def check_script(queries: List[Tuple[int, str]],
                 parsed: Dict[int, List[exp.Expression]],
                 optimise: bool,
                 assume_exists: Optional[Set[str]] = None) -> List[Violation]:
    """Cross-statement checks: temp table lifecycle and base table scan counts."""
    violations: List[Violation] = []

    def add(rule: str, message: str, severity: Optional[str] = None):
        category, default_severity, _ = RULES[rule]
        if category == "decomposition" and optimise:
            return
        violations.append(
            Violation(rule=rule, message=message, severity=severity or default_severity)
        )

    statement_nums = {num for num, _ in queries}
    created: Dict[str, int] = {name.lower(): 0 for name in (assume_exists or set())}
    external = set(created)
    last_ref: Dict[str, int] = {}
    drop_only_stmts: Set[int] = set()
    drops: Dict[str, List[int]] = {}
    reported_scope: Set[str] = set()
    base_table_stmts: Dict[str, List[int]] = {}

    for query_num, query_text in queries:
        clean = strip_noise(query_text)
        dropped_here = set(_drop_targets(clean))
        if is_drop_only(clean):
            drop_only_stmts.add(query_num)

        creations = {m.lower() for m in CREATE_TEMP_RE.findall(clean)}
        creations |= {m.lower() for m in INTO_TEMP_RE.findall(clean)}
        for name in creations:
            created.setdefault(name, query_num)

        for raw in TEMP_NAME_RE.findall(clean):
            name = raw.lower()
            if name in dropped_here or name in creations:
                continue
            last_ref[name] = query_num
            if name not in created and name not in reported_scope:
                reported_scope.add(name)
                add("TEMP_SCOPE",
                    f"query #{query_num} references {raw} before any statement creates it")

        for name in dropped_here:
            # a DROP ... IF EXISTS guard before the create is fine and not counted
            if name in created and created[name] < query_num:
                drops.setdefault(name, []).append(query_num)

        for statement in parsed.get(query_num, []):
            cte_names = {
                cte.alias_or_name.lower()
                for with_ in ([statement] if isinstance(statement, exp.With) else [])
                + [n for n in descendants(statement) if isinstance(n, exp.With)]
                for cte in with_.expressions
            }
            for table in statement.find_all(exp.Table):
                if is_temp_table(table):
                    continue
                key = table_key(table)
                if not key or table.name.lower() in cte_names:
                    continue
                if key.split(".")[0] in SYSTEM_SCHEMAS or (table.db or "").lower() in SYSTEM_SCHEMAS:
                    continue
                if table.name.lower().startswith("sys") or table.name.startswith("@"):
                    continue
                base_table_stmts.setdefault(key, []).append(query_num)

    for name, create_stmt in sorted(created.items()):
        if name in external:
            continue  # created outside this script (--assume-exists)
        name_drops = drops.get(name, [])
        if not name_drops:
            add("DECOMP_TEMP_NOT_DROPPED",
                f"{name} is created in query #{create_stmt} but never dropped")
            continue
        if len(name_drops) > 1:
            add("DECOMP_TEMP_DROPPED_TWICE",
                f"{name} is dropped in queries "
                f"{', '.join('#' + str(d) for d in name_drops)}")
        last_use = max(last_ref.get(name, create_stmt), create_stmt)
        first_drop = name_drops[0]
        intervening = [
            n for n in range(last_use + 1, first_drop)
            if n not in drop_only_stmts and n in statement_nums
        ]
        if intervening:
            add("DECOMP_DROP_LATE",
                f"{name} is last used in query #{last_use} but only dropped in "
                f"query #{first_drop}, after {len(intervening)} unrelated statement(s) "
                f"(first: #{intervening[0]}) - drop it at the first opportunity")

    for key, stmts in sorted(base_table_stmts.items()):
        if len(stmts) > 1:
            where = ", ".join("#" + str(s) for s in sorted(set(stmts)))
            add("DECOMP_BASE_TABLE_SCAN",
                f"base table {key} is referenced {len(stmts)} times (queries {where}) - "
                f"scan it once into a population #temp table and derive subsets from that")

    return violations


# ---------------------------------------------------------------------------
# Per-query driver
# ---------------------------------------------------------------------------

def check_query(query_num: int, query_text: str, optimise: bool = False
                ) -> Tuple[QueryResult, List[exp.Expression]]:
    """Check a single query for legality in SQL Server 2016."""
    result = QueryResult(query_num=query_num, query_text=query_text)

    # Step 1: Try to parse with sqlglot
    try:
        parsed = [p for p in sqlglot.parse(query_text, dialect=DIALECT) if p is not None]
        if not parsed:
            result.parse_error = "Parser returned no results (empty query or complete parse failure)"
            result.is_valid = False
            return result, []

        for statement in parsed:
            analyze_expression_tree(statement, result.features, result.warnings)

    except sqlglot.errors.ParseError as e:
        result.parse_error = str(e)

        # Check for common patterns
        missing_select = detect_missing_select_keyword(query_text)
        if missing_select:
            result.parse_error += f"\n  -> Likely cause: {missing_select}"

        result.is_valid = False
        return result, []

    # Step 2: Check for features not available in SQL Server 2016
    feature_issues = check_query_features(query_text)
    if feature_issues:
        for issue in feature_issues:
            result.warnings.append(f"Feature: {issue}")
            result.is_valid = False

    # Step 3: T-SQL constraint / house-style rule checks
    seen: Set[Tuple[str, str]] = set()
    for statement in parsed:
        try:
            for violation in Checker(statement, query_text, optimise).run():
                key = (violation.rule, violation.message)
                if key in seen:
                    continue
                seen.add(key)
                result.violations.append(violation)
        except RecursionError:
            result.warnings.append("Statement too deeply nested for semantic checks")

    if result.errors:
        result.is_valid = False

    # Step 4: Check for supported syntax patterns
    upper_query = strip_noise(query_text).upper()

    if re.search(r'#\w+', upper_query):
        result.features.append("Local temp table (#table) - SQL Server 2000+")

    if re.search(r'##\w+', upper_query):
        result.features.append("Global temp table (##table) - SQL Server 2000+")

    if re.search(r'SELECT\s+.*\s+INTO\s+', upper_query):
        result.features.append("SELECT INTO - SQL Server 2000+")

    if re.search(r'DROP\s+TABLE\s+IF\s+EXISTS', upper_query):
        result.features.append("DROP TABLE IF EXISTS - SQL Server 2016+")

    if re.search(r'\bsp_executesql\b', upper_query):
        result.features.append("sp_executesql - SQL Server 2000+")

    if re.search(r'\b(EXEC|EXECUTE)\b', upper_query):
        result.features.append("EXEC/EXECUTE - SQL Server 2000+")

    return result, parsed


def generate_report(report: CheckReport, verbose: bool = False, optimise: bool = False) -> str:
    """Generate a formatted report."""
    mode = ("OPTIMISE (efficient constructs allowed; decomposition rules off)"
            if optimise else "DEBUG (house-style temp-table rules enforced)")
    advisories = sum(len(r.advisories) for r in report.results) + \
        len([v for v in report.script_violations if v.severity != ERROR])

    lines = []
    lines.append("=" * 80)
    lines.append("SQL SERVER 2016 QUERY LEGALITY CHECK REPORT")
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"Mode:                   {mode}")
    lines.append(f"Total queries analyzed: {report.total_queries}")
    lines.append(f"Valid queries:          {report.valid_queries}")
    lines.append(f"Invalid queries:        {report.invalid_queries}")
    lines.append(f"Advisories (warnings):  {advisories}")
    lines.append("")

    if report.invalid_queries > 0:
        lines.append("-" * 80)
        lines.append("INVALID QUERIES:")
        lines.append("-" * 80)

        for r in report.results:
            if not r.is_valid:
                lines.append("")
                lines.append(f"  Query #{r.query_num}: INVALID")
                if r.parse_error:
                    lines.append(f"    Parse Error:")
                    for err_line in r.parse_error.split('\n'):
                        lines.append(f"      {err_line}")
                if r.warnings:
                    lines.append(f"    Feature Issues:")
                    for w in r.warnings:
                        lines.append(f"      - {w}")
                if r.errors:
                    lines.append(f"    Rule Violations:")
                    for v in r.errors:
                        lines.append(f"      - {v}")
                if verbose and r.query_text:
                    first_lines = r.query_text.split('\n')[:5]
                    lines.append(f"    Query preview:")
                    for fl in first_lines:
                        lines.append(f"      {fl}")
                    if len(r.query_text.split('\n')) > 5:
                        lines.append(f"      ... ({len(r.query_text.split(chr(10)))} lines total)")

        lines.append("")

    advisory_results = [r for r in report.results if r.advisories]
    if advisory_results:
        lines.append("-" * 80)
        lines.append("ADVISORIES (reviewed by hand; they do not fail the check):")
        lines.append("-" * 80)
        for r in advisory_results:
            lines.append("")
            lines.append(f"  Query #{r.query_num}:")
            for v in r.advisories:
                lines.append(f"      - {v}")
        lines.append("")

    if report.script_violations:
        lines.append("-" * 80)
        lines.append("SCRIPT-LEVEL CHECKS (temp table lifecycle / base table scans):")
        lines.append("-" * 80)
        lines.append("")
        for v in report.script_violations:
            marker = "ERROR  " if v.severity == ERROR else "WARNING"
            lines.append(f"  {marker} {v}")
        lines.append("")

    if verbose:
        lines.append("-" * 80)
        lines.append("ALL QUERIES DETAIL:")
        lines.append("-" * 80)

        for r in report.results:
            lines.append("")
            status = "VALID" if r.is_valid else "INVALID"
            lines.append(f"  Query #{r.query_num}: {status}")
            if r.features:
                lines.append(f"    Features: {', '.join(r.features)}")
            if r.warnings:
                lines.append(f"    Warnings: {', '.join(r.warnings)}")
            for v in r.violations:
                lines.append(f"    {v.severity.upper()}: {v}")

    lines.append("")
    lines.append("=" * 80)
    return '\n'.join(lines)


def parse_bool(value: Optional[str]) -> bool:
    if value is None:
        return True
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise SystemExit(f"Error: --optimise expects true/false (or 1/0), got: {value!r}")


def print_rules():
    print("Rule catalogue - severity applies when the rule's mode is active")
    print("=" * 80)
    by_category: Dict[str, List[str]] = {}
    for rule, (category, severity, description) in RULES.items():
        by_category.setdefault(category, []).append(
            f"  {severity.upper():7} {rule:28} {description}"
        )
    for category in sorted(by_category):
        note = ""
        if category == "decomposition":
            note = "  (enforced only WITH --debug)"
        elif category == "efficiency":
            note = "  (enforced by default; off under --debug)"
        print(f"\n{category}{note}")
        for line in sorted(by_category[category]):
            print(line)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Check SQL Server 2016 query legality using sqlglot"
    )
    parser.add_argument("sql_file", nargs="?", help="Path to the SQL file to check")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed output for all queries")
    parser.add_argument("--fail-fast", action="store_true",
                        help="Stop at first invalid query")
    parser.add_argument("--debug", action="store_true",
                        help="enforce the house-style temp-table decomposition rules "
                             "(no CTEs, no derived tables, no IN/EXISTS subqueries) and "
                             "turn off efficiency advisories. Off by default.")
    parser.add_argument("--optimise", "--optimize", dest="optimise", nargs="?",
                        const="true", default="true", metavar="true|false",
                        help="efficiency mode - CTEs, derived tables and subqueries are "
                             "allowed and EFF_* advisories are reported. This is now the "
                             "DEFAULT; the flag is kept for compatibility. --optimise false "
                             "is equivalent to --debug.")
    parser.add_argument("--assume-exists", default="", metavar="#a,#b",
                        help="Comma-separated temp tables that are created outside this "
                             "script (suppresses TEMP_SCOPE and lifecycle rules for them)")
    parser.add_argument("--metadata", metavar="DIR", default=None,
                        help="metadata corpus (built by tools/sqlserver-metadata) to "
                             "check column existence against. Off when omitted.")
    parser.add_argument("--list-rules", action="store_true",
                        help="Print the rule catalogue and exit")

    args = parser.parse_args()

    if args.list_rules:
        print_rules()
        sys.exit(0)

    if not args.sql_file:
        parser.error("the following arguments are required: sql_file")

    # House-style decomposition is opt-in via --debug. --optimise false is the
    # legacy spelling of the same request and still works.
    optimise = False if args.debug else parse_bool(args.optimise)

    file_path = Path(args.sql_file)
    if not file_path.exists():
        print(f"Error: File not found: {file_path}")
        sys.exit(1)

    print(f"Analyzing: {file_path}")
    print(f"sqlglot version: {sqlglot.__version__}")
    print(f"Dialect: TSQL (SQL Server)")
    print(f"Mode: {'default (efficiency)' if optimise else 'debug (temp-table decomposition)'}")
    print()

    queries = extract_queries_from_file(str(file_path))
    print(f"Extracted {len(queries)} queries from file")
    print()

    report = CheckReport(total_queries=len(queries))
    parsed_by_query: Dict[int, List[exp.Expression]] = {}
    checked: List[Tuple[int, str]] = []

    for query_num, query_text in queries:
        result, parsed = check_query(query_num, query_text, optimise=optimise)
        report.results.append(result)
        parsed_by_query[query_num] = parsed
        checked.append((query_num, query_text))

        if result.is_valid:
            report.valid_queries += 1
            status = "VALID"
        else:
            report.invalid_queries += 1
            status = "INVALID"

        print(f"  Query #{result.query_num}: {status}")

        if not result.is_valid:
            if result.parse_error:
                print(f"    Error: {result.parse_error[:200]}")
            for w in result.warnings:
                print(f"    Warning: {w}")
            for v in result.errors:
                print(f"    Violation: {v}")

        if args.fail_fast and not result.is_valid:
            print("\nStopped at first invalid query (--fail-fast)")
            break

    assume_exists = {
        name.strip().lower() for name in args.assume_exists.split(",") if name.strip()
    }
    report.script_violations = check_script(checked, parsed_by_query, optimise, assume_exists)
    report.script_violations.extend(check_alias_columns(checked, parsed_by_query))

    # Column existence checks against the object corpus
    if args.metadata:
        metadata_dir = Path(args.metadata)
        if not metadata_dir.is_dir():
            print(f"Error: metadata directory not found: {metadata_dir}")
            sys.exit(2)
        print(f"Building metadata catalogue from {metadata_dir}...")
        catalog = build_metadata_catalog(metadata_dir)
        complete = len(catalog.complete_objects)
        print(f"  Indexed {len(catalog)} tables/views "
              f"({complete} with a readable column list, "
              f"{len(catalog) - complete} without)")

        column_violations = check_columns_against_metadata(parsed_by_query, catalog)
        report.script_violations.extend(column_violations)

        if column_violations:
            print(f"  Found {len(column_violations)} table(s)/view(s) referenced with "
                  f"columns that are not in the metadata catalogue")
    else:
        print("Column existence checks: off (pass --metadata DIR to enable)")

    print()
    print(generate_report(report, verbose=args.verbose, optimise=optimise))

    failed = report.invalid_queries > 0 or bool(report.script_errors)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
