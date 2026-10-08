#!/usr/bin/env python3
"""One-shot river layout: the only river-layout command a working session runs.

    python3 tools/river-layout/river.py FILE.sql [FILE.sql ...]

Per file: Pass 1, Pass 2, then the proof and the gate.

  proof  the code is unchanged - the same characters outside whitespace, and the same
         code once comments and literals are blanked (the query checker's own
         strip_noise), so no line was joined into a comment. If the proof fails the
         original file is put back.
  gate   a second Pass 1 moves nothing, and the audit reports 0 findings.

Layout never blocks delivery. When the gate or the proof fails, or a pass crashes,
this logs the case - a copy of the input, the pass output, the audit findings - under
tools/river-layout/issues/ for a later, dedicated river-layout session, prints LOGGED,
and exits 0. The file is left legal (formatted where that was safe, original where it
was not); the session ships it as is and does not hand-format.

Exit 1 only when a file cannot be read.
"""
import datetime
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ISSUES_DIR = HERE / "issues"
ISSUES_MD = ISSUES_DIR / "ISSUES.md"
sys.path.insert(0, str(HERE.parent / "sql-server-2016-query-check"))
from check_queries import strip_noise   # noqa: E402  (stdlib + vendored sqlglot only)

FRONT_MATTER = """---
title: "river-layout issues - cases river.py could not settle"
audience: [llm, human]
published: {date}
subject: "Append-only log, written by river.py: each entry is a SQL file the river passes could not settle or could not format safely, with the input kept beside it."
outcomes:
  - "Reproduce each case from its saved input with the commands in the entry."
  - "Fix the passes in a dedicated session, then delete the fixed entries and their inputs."
generated_by: "tools/river-layout/river.py"
---

# river-layout issues

Written by `river.py`; working sessions only append. A dedicated session fixes the passes
(`_river_core.py`, `_river_layout.py`, `_align_items.py`), re-runs every saved input here,
and removes the entries that now settle. The README's fixed-point checks still apply.
"""


def run(script, *args):
    proc = subprocess.run([sys.executable, str(HERE / script), *args],
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def count(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def code_unchanged(before, after):
    squash = lambda s: re.sub(r"\s+", "", s)
    return squash(before) == squash(after) and squash(strip_noise(before)) == squash(strip_noise(after))


def log_issue(path, original, problem, evidence):
    digest = hashlib.sha1(original.encode()).hexdigest()[:10]
    ISSUES_DIR.mkdir(exist_ok=True)
    saved = ISSUES_DIR / f"{digest}_{path.name}"
    if saved.exists():
        return saved, False                       # this exact input is already logged
    saved.write_text(original)
    if not ISSUES_MD.exists():
        ISSUES_MD.write_text(FRONT_MATTER.format(date=datetime.date.today().isoformat()))
    rel = saved.relative_to(HERE.parent.parent)
    with ISSUES_MD.open("a") as out:
        out.write(f"\n## {datetime.datetime.now():%Y-%m-%d %H:%M} {path.name} ({digest})\n\n"
                  f"- **Problem:** {problem}\n"
                  f"- **Source file:** `{path}`\n"
                  f"- **Input kept:** `{rel}`\n"
                  f"- **Reproduce:** `cp {rel} /tmp/r.sql && python3 tools/river-layout/river.py /tmp/r.sql`\n\n"
                  f"```text\n{evidence.strip()[:4000]}\n```\n")
    return saved, True


def river(path):
    original = path.read_text()
    backup = path.with_name(path.name + ".river-bak")
    shutil.copyfile(path, backup)
    try:
        rc1, out1 = run("_river_layout.py", str(path))
        rc2, out2 = run("_align_items.py", str(path))
        rc3, again = run("_river_layout.py", "--report", str(path))
        rc4, audit = run("_audit_layout.py", str(path))
        evidence = "\n".join([out1, out2, again, audit])
        after = path.read_text()
        if any((rc1, rc2, rc3, rc4)):
            shutil.copyfile(backup, path)
            problem = "a pass crashed; the original file was restored"
        elif not code_unchanged(original, after):
            shutil.copyfile(backup, path)
            problem = "the passes changed code, not just whitespace; the original file was restored"
        else:
            moves = count(r"would move (\d+) lines", again)
            joins = count(r"joined (\d+)", again)
            findings = count(r": (\d+) findings", audit)
            if not moves and not joins and not findings:
                moved = count(r"moved (\d+) lines", out1)
                print(f"{path}: settled ({moved} lines moved, 0 findings)")
                return
            parts = [f"a second Pass 1 would move {moves} lines" if moves else "",
                     f"join {joins}" if joins else "",
                     f"the audit reports {findings} findings" if findings else ""]
            problem = ("not settled: " + "; ".join(p for p in parts if p)
                       + " (formatted file kept - code unchanged)")
        saved, new = log_issue(path, original, problem, evidence)
        print(f"{path}: LOGGED - {problem}.\n"
              f"  {'Logged' if new else 'Already logged'} in tools/river-layout/issues/ISSUES.md "
              f"(input kept as {saved.name}).\n"
              f"  Layout is done for this file: do not hand-format it, do not re-run, do not open "
              f"the river-layout code. Ship it once check_queries.py passes.")
    finally:
        backup.unlink(missing_ok=True)


def main(argv):
    files = [Path(a) for a in argv if not a.startswith("-")]
    if not files:
        print(__doc__)
        return 2
    bad = 0
    for path in files:
        if not path.is_file():
            print(f"{path}: cannot read")
            bad = 1
            continue
        river(path)
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
