#!/usr/bin/env python3
"""Shared scope model for the river-layout passes and the audit (SQL-STYLE.md).

A statement is split on ';' at paren depth 0 (block comments, line comments and string
literals blanked first - the query checker's convention). A statement is GOVERNED when
its first code line starts with SELECT, WITH, INSERT, UPDATE or DELETE; everything else
(DDL, IF guards, EXEC, DECLARE, SET, DROP) is left as authored by every pass.

Inside a governed statement every line belongs to the innermost scope open at its start:

  stmt   the statement itself
  paren  a '(' ... ')' pair that spans lines - keyed on the PAIR, not on paren depth, so
         two sibling subqueries at one depth are two scopes with two bases
  case   a CASE ... END that spans lines: its own river (CASE/WHEN/THEN/ELSE/END, 4 wide),
         with AND/OR inside a WHEN right-aligned to it

Per scope:  base   column the longest keyword starts in
            width  length of the longest keyword in the scope (0 if it has none)
            river  base + width - 1, the column every keyword ends in
            A      the argument column, river + 2 (= base when the scope has no keyword)

The base of a stmt scope is the block indent (4 per BEGIN level, 4 in a proc body), or the
authored indent at file level. A paren scope's base is one column right of its '(', and a
case scope's base is its CASE token - both measured AFTER the opener's line is placed.
"""
import re

TOP_KEYWORDS = [
    "FULL OUTER JOIN", "RIGHT OUTER JOIN", "LEFT OUTER JOIN", "CROSS JOIN",
    "INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN", "CROSS APPLY", "OUTER APPLY",
    "GROUP BY", "ORDER BY", "PARTITION BY", "UNION ALL", "UNION", "EXCEPT", "INTERSECT",
    "SELECT", "FROM", "WHERE", "INTO", "HAVING", "ON", "AND", "OR", "VALUES",
    "SET", "EXEC", "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "TRUNCATE",
    "WITH", "AS",
]
TOP_KEYWORDS.sort(key=len, reverse=True)
CASE_KEYWORDS = ["CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR"]
SCOPE_START = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")
NO_ARGUMENT = (";", "CASE", "END", "UNION", "UNION ALL", "EXCEPT", "INTERSECT")
BODY_INDENT = 4

TOKEN = re.compile(r"\(|\)|\bCASE\b|\bEND\b", re.I)
BLOCK_TOKEN = re.compile(r"\bBEGIN\b(?!\s+(TRAN|TRANSACTION|DISTRIBUTED|DIALOG|CONVERSATION)\b)"
                         r"|\bCASE\b|\bEND\b", re.I)
PROC_START = re.compile(r"^\s*CREATE\s+(OR\s+ALTER\s+)?PROC(EDURE)?\b", re.I)
BATCH_END = re.compile(r"^\s*GO\s*$", re.I)


def indent_of(text):
    return len(text) - len(text.lstrip(" "))


def scan(lines):
    """(view, starts_in_string): the code text of each line with string-literal contents,
    line comments and block comments blanked (same length, so columns hold), and whether
    each line starts inside a multi-line string literal (such a line is never moved)."""
    view, in_string_at_start = [], []
    in_block = in_str = False
    for line in lines:
        in_string_at_start.append(in_str)
        buf, i, n = [], 0, len(line)
        while i < n:
            ch = line[i]
            if in_block:
                if line.startswith("*/", i):
                    buf.append("  ")
                    i += 2
                    in_block = False
                else:
                    buf.append(" ")
                    i += 1
            elif in_str:
                if line.startswith("''", i):
                    buf.append("  ")
                    i += 2
                elif ch == "'":
                    buf.append("'")
                    i += 1
                    in_str = False
                else:
                    buf.append(" ")
                    i += 1
            elif line.startswith("--", i):
                buf.append(" " * (n - i))
                break
            elif line.startswith("/*", i):
                buf.append("  ")
                i += 2
                in_block = True
            else:
                if ch == "'":
                    in_str = True
                buf.append(ch)
                i += 1
        view.append("".join(buf))
    return view, in_string_at_start


def code_view(lines):
    return scan(lines)[0]


def block_comment_mask(lines):
    """True for a line inside a block comment or opening one: such lines never move."""
    mask, in_block = [], False
    for line in lines:
        text = re.sub(r"--.*$", "", re.sub(r"'[^']*'", "''", line))
        starts, ends = text.count("/*"), text.count("*/")
        mask.append(in_block or starts > 0)
        in_block = in_block or starts > 0
        if ends:
            in_block = False
    return mask


def leading_keyword(text, keywords=TOP_KEYWORDS):
    """(indent, keyword, length) for a line of code view that starts with a keyword."""
    stripped = text.lstrip(" ")
    indent = len(text) - len(stripped)
    if not stripped:
        return None
    if stripped.startswith(","):
        return indent, ",", 1
    if stripped.startswith(";") and stripped.rstrip(";").strip() == "":
        return indent, ";", 1
    upper = stripped.upper()
    for kw in keywords:
        # the keyword, then end of line or any character that cannot continue an identifier
        if upper.startswith(kw) and (len(upper) == len(kw) or not (
                upper[len(kw)].isalnum() or upper[len(kw)] in "_@#$.[")):
            return indent, kw, len(kw)
    return None


def statements(view):
    start, depth = 0, 0
    for i, code in enumerate(view):
        depth += code.count("(") - code.count(")")
        if depth <= 0 and re.search(r";\s*$", code):
            yield start, i
            start, depth = i + 1, 0
    if start < len(view):
        yield start, len(view) - 1


class Scope:
    def __init__(self, kind, parent, open_line=None, open_col=None):
        self.kind, self.parent = kind, parent
        self.open_line, self.open_col = open_line, open_col
        self.close_line = self.close_col = None
        self.lines = []
        self.width = 0
        # authored positions; Pass 1 replaces them once the opener's line is placed
        self.final_open = open_col
        self.base = None if open_col is None else open_col + (1 if kind == "paren" else 0)

    @property
    def keywords(self):
        return CASE_KEYWORDS if self.kind == "case" else TOP_KEYWORDS

    def keyword(self, code):
        kw = leading_keyword(code, self.keywords)
        if kw is None or kw[1] == "AS":
            return None
        return kw

    @property
    def arg_col(self):
        return self.base + self.width + 1 if self.width else self.base


class Statement:
    """One governed statement: its first code line, its scopes, and each line's scope."""

    def __init__(self, s, e, first, root, scope_at, scopes):
        self.s, self.e, self.first = s, e, first
        self.root, self.scope_at, self.scopes = root, scope_at, scopes

    def closes(self, i, code):
        """The scope line i closes with its first token (')' or a CASE's END), or None."""
        scope = self.scope_at.get(i)
        if scope is None or scope.close_line != i:
            return None
        lead = indent_of(code)
        if scope.close_col == lead:
            return scope
        return None


def governed_statements(lines, view=None, blocky=None):
    if view is None:
        view = code_view(lines)
    if blocky is None:
        blocky = block_comment_mask(lines)
    for s, e in statements(view):
        first = next((i for i in range(s, e + 1)
                      if not blocky[i] and view[i].strip()), None)
        if first is None:
            continue
        kw = leading_keyword(view[first])
        if kw is None or kw[1] not in SCOPE_START:
            continue
        model = build(view, first, e)
        if model is not None:
            root, scope_at, scopes = model
            yield Statement(s, e, first, root, scope_at, scopes)


def build(view, first, last):
    root = Scope("stmt", None)
    stack, scope_at, scopes = [root], {}, [root]
    for i in range(first, last + 1):
        scope_at[i] = stack[-1]
        for m in TOKEN.finditer(view[i]):
            tok = m.group().upper()
            if tok in ("(", "CASE"):
                scope = Scope("paren" if tok == "(" else "case", stack[-1], i, m.start())
                stack.append(scope)
                scopes.append(scope)
            elif tok == ")":
                if stack[-1].kind != "paren":
                    return None                         # unbalanced: leave as authored
                scope = stack.pop()
                scope.close_line, scope.close_col = i, m.start()
            elif tok == "END" and stack[-1].kind == "case":
                scope = stack.pop()
                scope.close_line, scope.close_col = i, m.start()
    if len(stack) != 1:
        return None
    for i, scope in scope_at.items():
        scope.lines.append(i)
    return root, scope_at, [sc for sc in scopes
                            if sc is root or sc.open_line != sc.close_line]


def block_bases(lines, view):
    """Body indent at each line start: 4 per open BEGIN block, 4 inside a proc body with
    no BEGIN, None at file level (the statement keeps its authored indent)."""
    bases, stack, in_proc = [], [], False
    for raw, code in zip(lines, view):
        if PROC_START.match(code):
            in_proc = True
        if BATCH_END.match(code):
            in_proc, stack = False, []
        blocks = stack.count("B")
        bases.append(BODY_INDENT * blocks if blocks else (BODY_INDENT if in_proc else None))
        for m in BLOCK_TOKEN.finditer(code):
            tok = m.group().upper()
            if tok.startswith("BEGIN"):
                stack.append("B")
            elif tok == "CASE":
                stack.append("C")
            elif stack:
                stack.pop()
    return bases
