#!/usr/bin/env python3
"""
Build the metadata corpus that check_queries.py --metadata reads, from the
CSV that export_objects.sql produces.

CSV columns (0-indexed):
  0 = database name
  1 = object type (PROC / TABLE / VIEW)
  2 = schema name
  3 = object name
  4 = timestamp (bigint)
  5 = SQL body with _LINEBREAK_ markers
  6 = extra column (always NULL)

Target structure:
  <out>/
  ├── <database>/
  │   ├── PROCEDURE/   ← maps from PROC
  │   ├── TABLE/
  │   └── VIEW/
  │       └── [<database>].[<schema>].[<name>].sql

Each .sql file starts with a short header block (database, object, type,
timestamp, source), followed by the SQL body with _LINEBREAK_ converted to
real newlines.
"""

import csv
import os
import sys
import shutil

# Mapping from CSV type codes to folder names
# Empty file written into every corpus this script builds; see main().
MARKER = ".sqlserver-metadata"

TYPE_MAP = {
    "PROC": "PROCEDURE",
    "TABLE": "TABLE",
    "VIEW": "VIEW",
}


def build_header_line(db, schema, name, obj_type, timestamp, source_file):
    """Build a multi-line header block."""
    lines = [
        f"-- Database: {db}",
        f"-- Object: [{db}].[{schema}].[{name}]",
        f"-- Type: {obj_type}",
        f"-- Timestamp: {timestamp}",
        f"-- Source: {source_file}",
        "=" * 80,
        "",
    ]
    return "\n".join(lines)


def process_row(row, metadata_base, source_name):
    """Process a single CSV row and write the .sql file. Returns the output path or None."""
    db = row[0].strip()
    csv_type = row[1].strip()
    schema = row[2].strip()
    name = row[3].strip()
    timestamp = row[4].strip()
    sql_body = row[5].strip() if len(row) > 5 else ""

    # Skip rows with empty critical fields
    if not all([db, csv_type, schema, name]):
        return None

    # Map type to folder name
    folder_name = TYPE_MAP.get(csv_type)
    if folder_name is None:
        print(f"WARNING: Unknown type '{csv_type}' for {db}.{schema}.{name}, skipping", file=sys.stderr)
        return None

    # Clean the database name (bcp can leave trailing spaces on it)
    db_clean = db.rstrip()

    # Build directory path
    db_dir = os.path.join(metadata_base, db_clean)
    type_dir = os.path.join(db_dir, folder_name)
    os.makedirs(type_dir, exist_ok=True)

    # Build filename: [Database].[Schema].[Name].sql
    filename = f"[{db_clean}].[{schema}].[{name}].sql"
    filepath = os.path.join(type_dir, filename)

    # Convert _LINEBREAK_ to actual newlines
    sql_content = sql_body.replace("_LINEBREAK_", "\n")

    # Build display type for header
    type_display = {
        "PROC": "PROCEDURE",
        "TABLE": "TABLE",
        "VIEW": "VIEW",
    }.get(csv_type, csv_type.upper())
    header = build_header_line(db_clean, schema, name, type_display, timestamp, source_name)

    # Write file
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(sql_content)
        if not sql_content.endswith("\n"):
            f.write("\n")

    return filepath


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Build a metadata corpus from the CSV export_objects.sql "
                    "produces (DELETES and rebuilds the whole output directory).",
        epilog="Then: check_queries.py FILE.sql --metadata OUT")
    ap.add_argument("--csv", required=True,
                    help="input CSV exported from SQL Server")
    ap.add_argument("--out", required=True,
                    help="output directory, removed and recreated")
    args = ap.parse_args()
    CSV_PATH, METADATA_BASE = args.csv, args.out

    if not os.path.exists(CSV_PATH):
        print(f"ERROR: {CSV_PATH} not found", file=sys.stderr)
        sys.exit(1)

    # Set CSV field size limit for large bodies
    csv.field_size_limit(2000000)

    # Read all rows
    print(f"Reading {CSV_PATH}...", file=sys.stderr)
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)

    print(f"Total rows: {len(rows)}", file=sys.stderr)

    # Remove the old corpus, but only one this script built (it leaves a
    # marker file), so a mistyped --out cannot wipe an unrelated folder.
    marker = os.path.join(METADATA_BASE, MARKER)
    if os.path.isdir(METADATA_BASE) and os.listdir(METADATA_BASE) and not os.path.exists(marker):
        print(f"ERROR: {METADATA_BASE} is not empty and has no {MARKER} marker; "
              f"refusing to delete it", file=sys.stderr)
        sys.exit(1)
    if os.path.exists(METADATA_BASE):
        print(f"Removing old corpus at {METADATA_BASE}...", file=sys.stderr)
        shutil.rmtree(METADATA_BASE)

    # Create fresh base directory
    os.makedirs(METADATA_BASE, exist_ok=True)
    open(marker, "w").close()

    # Process each row
    success = 0
    skipped = 0
    errors = 0
    dupes = 0

    # Track seen filenames for duplicate detection
    seen = set()

    for i, row in enumerate(rows):
        if not row or len(row) < 6:
            skipped += 1
            continue

        try:
            result = process_row(row, METADATA_BASE, os.path.basename(CSV_PATH))
            if result is None:
                skipped += 1
            else:
                # Duplicate check
                if result in seen:
                    print(f"WARNING: Duplicate file {result}, skipping", file=sys.stderr)
                    os.remove(result)
                    dupes += 1
                else:
                    seen.add(result)
                    success += 1
        except Exception as e:
            print(f"ERROR processing row {i}: {e}", file=sys.stderr)
            errors += 1

    # Summary
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"SUMMARY", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    print(f"  Successfully created : {success}", file=sys.stderr)
    print(f"  Skipped (bad rows)   : {skipped}", file=sys.stderr)
    print(f"  Duplicates           : {dupes}", file=sys.stderr)
    print(f"  Errors               : {errors}", file=sys.stderr)
    print(f"  Total output files   : {success - dupes}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)


if __name__ == "__main__":
    main()
