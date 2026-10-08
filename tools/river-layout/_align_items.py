#!/usr/bin/env python3
"""Pass 2 of the river layout, run after _river_layout.py: the AS column. Idempotent.

Per scope of every GOVERNED statement (the _river_core.py model: the statement, each
multi-line '(' ... ')' pair, each multi-line CASE ... END), every `expr AS alias` line
lines up its AS one space past the longest expression in that scope (a trailing comment
does not count). A line that opens with the ')' or END closing a nested scope - `) AS x`,
`END AS x` - is an item of the ENCLOSING scope, so a multi-line CASE or subquery takes its
alias in the same column as the one-line items around it.

Only the gap before AS changes, so Pass 1's placement (which reads indentation and the
columns of '(' and CASE, never line length) is unaffected and stays a fixed point.

Usage: _align_items.py FILE [FILE...]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _river_core import block_comment_mask, governed_statements, scan   # noqa: E402

AS_LINE = re.compile(r"^(?P<expr>.*\S)\s+AS\s+(?P<alias>\[?[A-Za-z_][A-Za-z_0-9\]]*)\s*$")


def align(path):
    lines = Path(path).read_text().split("\n")
    view, in_str = scan(lines)
    blocky = block_comment_mask(lines)
    aligned = 0
    for st in governed_statements(lines, view, blocky):
        groups = {}
        for i in range(st.first, st.e + 1):
            if blocky[i] or in_str[i] or not lines[i].strip() or lines[i].lstrip().startswith("--"):
                continue
            scope = st.closes(i, view[i]) or None
            scope = scope.parent if scope is not None else st.scope_at[i]
            groups.setdefault(id(scope), []).append(i)
        for rows in groups.values():
            as_rows = []
            for i in rows:
                code = view[i].rstrip()
                m = AS_LINE.match(code)
                if m:
                    as_rows.append((i, len(m.group("expr"))))
            if not as_rows:
                continue
            target = max(length for _, length in as_rows) + 1
            for i, _ in as_rows:
                code_len = len(view[i].rstrip())
                m = AS_LINE.match(lines[i][:code_len])
                expr, alias = m.group("expr"), m.group("alias")
                tail = lines[i][code_len:]
                new = (expr + " " * max(target - len(expr), 1) + "AS " + alias + tail).rstrip()
                if new != lines[i]:
                    lines[i] = new
                    aligned += 1
    Path(path).write_text("\n".join(lines))
    print(f"{path}: {aligned} AS aliases aligned")


for arg in sys.argv[1:]:
    align(arg)
