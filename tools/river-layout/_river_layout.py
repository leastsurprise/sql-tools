#!/usr/bin/env python3
"""Pass 1 of the river layout (SQL-STYLE.md): the keyword river, the argument column,
comment placement and bracket mirroring, per scope (see _river_core.py for the model).

For every GOVERNED statement (first code line SELECT / WITH / INSERT / UPDATE / DELETE):

  I1  a keyword with no argument on its line takes the next code line as its argument
      (SELECT / WHERE / ON ... never stand alone; the comment lines between them stay,
      below the joined line). CASE, END, ';' and UNION stand alone by design
  river  every keyword in a scope right-aligns to the scope's river
  I2  every argument, continuation line and nested-scope opener starts at the scope's
      argument column A = river + 2
  I3  a line that opens with ')' sits in the column of the '(' it closes; a CASE's END
      right-aligns to the CASE river
  I4  the comment block directly above the statement left-aligns to its first keyword
  I5  a comment inside a scope sits at that scope's A
  I6  a multi-line CASE is a scope of its own: WHEN/THEN/ELSE/END and the AND/OR inside a
      WHEN right-align to the CASE token's river (4 wide)

Whitespace only: indentation changes, and an I1 join removes one line break. No token,
literal or name is touched, block comments and multi-line string literals never move,
and every other statement keeps the layout it was written with. Idempotent: a second run
reports 0 lines moved, 0 joined.

Usage: _river_layout.py [--report] FILE   (--report: say what would change, write nothing)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _river_core import (NO_ARGUMENT, block_bases, block_comment_mask,   # noqa: E402
                         governed_statements, indent_of, scan)


def is_comment(raw):
    return raw.lstrip().startswith("--")


def joins(lines):
    """I1: (keyword line, argument line) pairs to join, bottom-up."""
    view, in_str = scan(lines)
    blocky = block_comment_mask(lines)
    found = []
    for st in governed_statements(lines, view, blocky):
        for i in range(st.first, st.e + 1):
            scope = st.scope_at[i]
            if blocky[i] or in_str[i] or scope.kind == "case":
                continue
            kw = scope.keyword(view[i])
            if kw is None or kw[1] in NO_ARGUMENT or view[i].strip().upper() != kw[1]:
                continue
            if lines[i].rstrip() != view[i].rstrip():
                continue                                   # a trailing comment: leave it
            j = i + 1
            while j <= st.e and (not lines[j].strip() or (is_comment(lines[j]) and not blocky[j])):
                j += 1
            if (j > st.e or blocky[j] or in_str[j] or st.scope_at[j] is not scope
                    or scope.keyword(view[j]) or st.closes(j, view[j])):
                continue
            found.append((i, j))
    return sorted(found, reverse=True)


def apply_joins(lines):
    count = 0
    while True:
        pairs = joins(lines)
        if not pairs:
            return lines, count
        for i, j in pairs:
            lines[i] = lines[i].rstrip() + " " + lines[j].strip()
            del lines[j]
            count += 1


def place(lines):
    view, in_str = scan(lines)
    blocky = block_comment_mask(lines)
    bases = block_bases(lines, view)
    out = list(lines)
    for st in governed_statements(lines, view, blocky):
        root = st.root
        for scope in st.scopes:
            if scope.kind == "case":
                scope.width = 4
                continue
            kws = [scope.keyword(view[i]) for i in scope.lines
                   if not blocky[i] and not in_str[i] and not st.closes(i, view[i])]
            scope.width = max([k[2] for k in kws if k], default=0)
        if bases[st.first] is not None:
            root.base = bases[st.first]
        else:
            tops = [indent_of(lines[i]) for i in root.lines
                    if not blocky[i] and not in_str[i] and root.keyword(view[i])]
            root.base = min(tops) if tops else indent_of(lines[st.first])

        opened = {}
        for scope in st.scopes:
            if scope is not root:
                opened.setdefault(scope.open_line, []).append(scope)

        def want_for(i):
            scope = st.scope_at[i]
            closing = st.closes(i, view[i])
            if closing is not None and closing.kind == "paren":
                return closing.final_open
            if closing is not None and closing.kind == "case":
                return closing.base + closing.width - 3
            if is_comment(lines[i]):
                return scope.arg_col
            kw = scope.keyword(view[i])
            if kw is not None:
                return scope.base + scope.width - kw[2]
            return scope.arg_col

        for i in range(st.first, st.e + 1):
            if not out[i].strip():
                out[i] = ""
                continue
            if blocky[i] or in_str[i]:
                continue
            want = want_for(i)
            shift = want - indent_of(lines[i])
            out[i] = (" " * want + lines[i].strip()).rstrip()
            for scope in opened.get(i, []):
                if scope.kind == "paren":
                    scope.final_open = scope.open_col + shift
                    scope.base = scope.final_open + 1
                else:
                    scope.base = scope.open_col + shift

        # I4: the comment block directly above the statement starts where its first keyword does
        j = st.first - 1
        while j >= st.s and is_comment(lines[j]) and not blocky[j]:
            out[j] = " " * indent_of(out[st.first]) + lines[j].strip()
            j -= 1
    return out


def layout(lines):
    lines, joined = apply_joins(list(lines))
    out = place(lines)
    moved = sum(1 for a, b in zip(lines, out) if a != b)
    return out, moved, joined


def main():
    report = "--report" in sys.argv
    path = [a for a in sys.argv[1:] if not a.startswith("--")][0]
    lines = Path(path).read_text().split("\n")
    out, moved, joined = layout(lines)
    if not report:
        Path(path).write_text("\n".join(out))
    verb = "would move" if report else "moved"
    print(f"{path}: {verb} {moved} lines, joined {joined}")


if __name__ == "__main__":
    main()
