#!/usr/bin/env python3
"""Symmetry audit for the SQL-STYLE.md keyword river. Read-only: reports, never writes.

Measures the symmetry the layout is supposed to produce, per SCOPE of every governed
statement (the _river_core.py model: the statement, each multi-line '(' ... ')' pair -
keyed on the pair, so sibling subqueries at one depth are measured apart - and each
multi-line CASE ... END, which has its own river), and reports every place it is not there:

  SYM01 bare keyword        a scope keyword with no argument on its own line
                            (a lone SELECT / WHERE / ON / AND ...; CASE, END, ';' and
                            UNION are allowed to stand alone)
  SYM02 argument columns    more than one column starts arguments in one scope
  SYM03 unmirrored parens   a ')' not in the column of the '(' it closes
  SYM04 keyword river       a keyword in the scope ending in another column
  SYM05 intro comment       a comment block before a statement not left-aligned to the
                            statement's first keyword
  SYM06 in-scope comment    a comment inside a scope not at that scope's argument column
  SYM07 tabs                a tab in the indentation of a line outside a block comment

Governed statements are those whose first code line is SELECT / WITH / INSERT / UPDATE /
DELETE - the ones Pass 1 lays out. DDL, IF guards, EXEC and DECLARE are out of remit, as is
anything inside a block comment or a multi-line string literal.

Usage: _audit_layout.py FILE.sql [FILE.sql ...]      (per-line findings + totals)
       _audit_layout.py --summary FILE.sql           (totals only)
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _river_core import (NO_ARGUMENT, block_comment_mask, governed_statements,   # noqa: E402
                         scan)


def first_col(text, from_col=0):
    for i in range(from_col, len(text)):
        if not text[i].isspace():
            return i
    return None


def paren_pairs(view, first, last):
    """[(open_line, open_col, close_line, close_col)] by matching ( ) in the code view."""
    stack, pairs = [], []
    for i in range(first, last + 1):
        for off, ch in enumerate(view[i]):
            if ch == "(":
                stack.append((i, off))
            elif ch == ")" and stack:
                ol, oc = stack.pop()
                pairs.append((ol, oc, i, off))
    return pairs


def audit(path, verbose=True):
    lines = Path(path).read_text().split("\n")
    view, in_str = scan(lines)
    blocky = block_comment_mask(lines)
    findings = []

    def say(i, code, detail):
        findings.append((i + 1, code, detail))

    for n, line in enumerate(lines):
        if not blocky[n] and "\t" in line[:len(line) - len(line.lstrip())]:
            say(n, "SYM07", "tab in indentation")

    for st in governed_statements(lines, view, blocky):
        for ol, oc, cl, cc in paren_pairs(view, st.first, st.e):
            if ol == cl or oc == cc or not lines[cl].lstrip().startswith(")"):
                continue
            say(cl, "SYM03", f"'(' at col {oc+1} closes at col {cc+1} (opens: "
                             f"{lines[ol].strip()[:48]})")

        members = {}
        for i in range(st.first, st.e + 1):
            if blocky[i] or in_str[i] or not lines[i].strip():
                continue
            members.setdefault(id(st.scope_at[i]), (st.scope_at[i], []))[1].append(i)

        for scope, group in members.values():
            rivers, args, bare, comments = Counter(), Counter(), [], []
            for i in group:
                raw, code = lines[i], view[i]
                if raw.lstrip().startswith("--"):
                    comments.append(i)
                    continue
                if st.closes(i, code) is not None and scope.kind == "paren":
                    continue                            # a ')' mirrors its '(' - SYM03's job
                kw = scope.keyword(code)
                if kw is None:
                    col = first_col(raw)
                    if col is not None:
                        args[col] += 1
                    continue
                indent, kwname, klen = kw
                if kwname == ",":
                    col = first_col(raw, code.find(",") + 1)
                    if col is not None:
                        args[col] += 1
                    continue
                rivers[indent + klen] += 1
                if kwname == "END" and st.closes(i, code) is scope:
                    continue                            # `END AS x`: the enclosing item's alias
                col = first_col(code, indent + klen)
                if col is None:
                    if kwname not in NO_ARGUMENT:
                        bare.append((i, kwname))
                else:
                    args[col] += 1
            where = f"{scope.kind} scope"
            if len(rivers) > 1:
                for col, cnt in sorted(rivers.items()):
                    say(group[0], "SYM04", f"{where}: {cnt} keyword(s) end in col {col+1}, "
                                           f"river is {sorted(c+1 for c in rivers)}")
            for i, kwname in bare:
                say(i, "SYM01", f"{where}: bare {kwname} (no argument on its line)")
            if len(args) > 1:
                hist = ", ".join(f"col {c+1}x{n}" for c, n in sorted(args.items()))
                say(group[0], "SYM02", f"{where}: arguments start in {len(args)} columns: {hist}")
            if args:
                argcol = min(args)
                for i in comments:
                    ind = len(lines[i]) - len(lines[i].lstrip())
                    if ind != argcol:
                        say(i, "SYM06", f"comment at col {ind+1}, argument column is {argcol+1}")

        # the comment block directly introducing the statement
        kwcol = len(lines[st.first]) - len(lines[st.first].lstrip())
        kwname = st.root.keyword(view[st.first])[1]
        j = st.first - 1
        while j >= st.s and lines[j].lstrip().startswith("--") and not blocky[j]:
            ind = len(lines[j]) - len(lines[j].lstrip())
            if ind != kwcol:
                say(j, "SYM05", f"intro comment at col {ind+1}, statement's first "
                                f"keyword ({kwname}) at col {kwcol+1}")
            j -= 1

    findings = sorted(set(findings))
    counts = Counter(code for _, code, _ in findings)
    if verbose:
        for ln, code, detail in findings:
            print(f"{path}:{ln}: {code} {detail}")
    print(f"{path}: {len(findings)} findings " +
          " ".join(f"{c}={counts[c]}" for c in sorted(counts)))
    return findings


args = [a for a in sys.argv[1:] if not a.startswith("--")]
summary = "--summary" in sys.argv
total = Counter()
for f in args:
    found = audit(f, verbose=not summary)
    total.update(Counter(code for _, code, _ in found))
if len(args) > 1:
    print("TOTAL " + " ".join(f"{c}={total[c]}" for c in sorted(total)) + f" all={sum(total.values())}")
