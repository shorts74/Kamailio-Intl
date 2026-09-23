#!/usr/bin/env python3
"""
reconcile_schema.py -- auto-generates and applies ALTER TABLE ADD
COLUMN IF NOT EXISTS statements for every column defined in
schema.sql, run UNCONDITIONALLY on every install (not gated by any
checkpoint). This exists because schema.sql only uses CREATE TABLE
IF NOT EXISTS, which silently does nothing to an already-existing
table even if new columns have been added to the schema since that
table was first created -- confirmed as a real production bug
(platform_trunks missing gateway_group_id after several install/
upgrade cycles, causing "Internal Server Error" on pages that
queried it) and fixed with this reconciliation pass rather than a
one-off manual patch.

Safety:
- Never touches structural/identity columns (PRIMARY KEY, SERIAL) --
  if a table is missing those, something far more fundamental is
  wrong and this script deliberately does not attempt to fix it
- IF NOT EXISTS on every ADD COLUMN makes each statement a safe no-op
  if the column is already present
- Comment stripping and quote-aware comma-splitting are both required
  for correct parsing -- both were real bugs caught via testing
  against real Postgres before this was trusted (an early version
  mangled a comment into an adjacent column definition, and separately
  split a quoted default value like 'ulaw,alaw,g729' on the commas
  inside the string)
"""
import os
import re
import sys

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'schema.sql')

with open(SCHEMA_PATH) as f:
    content = f.read()

table_pattern = re.compile(
    r'CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);',
    re.DOTALL
)

alter_statements = []
skipped = []

for match in table_pattern.finditer(content):
    table_name = match.group(1)
    body = match.group(2)

    # Real, confirmed production bug fixed here: this script used to
    # only emit ALTER TABLE ADD COLUMN statements for each table's
    # columns, silently assuming the table itself already existed. If
    # a table was added to schema.sql after an existing deployment's
    # last install (confirmed live via a real Postgres simulation:
    # platform_subscriber_forwarding/platform_subscriber_numbers,
    # dropped to simulate an older deployment, then reconciled) the
    # ALTER TABLE statements themselves fail with "relation does not
    # exist" -- ADD COLUMN IF NOT EXISTS only makes the column
    # idempotent, not the table's own existence -- leaving the table
    # permanently missing and 500ing every page that queries it
    # (confirmed live: subscriber_detail -- exactly the "Numbers/
    # Forwarding page Internal Server Error" bug). Emitting the
    # table's own CREATE TABLE IF NOT EXISTS statement first fixes
    # this: safe to run even when the table already exists (no-op),
    # and actually creates it when it's genuinely missing.
    full_create_stmt = content[match.start():match.end()]
    alter_statements.append(full_create_stmt)

    body_no_comments = re.sub(r'--[^\n]*', '', body)

    lines = []
    depth = 0
    in_string = False
    current = ""
    for ch in body_no_comments:
        if ch == "'":
            in_string = not in_string
            current += ch
        elif ch == '(' and not in_string:
            depth += 1
            current += ch
        elif ch == ')' and not in_string:
            depth -= 1
            current += ch
        elif ch == ',' and depth == 0 and not in_string:
            lines.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        lines.append(current.strip())

    for line in lines:
        line = re.sub(r'\s+', ' ', line.strip())
        if not line:
            continue
        upper = line.upper()
        if upper.startswith('CONSTRAINT') or upper.startswith('PRIMARY KEY') or \
           upper.startswith('UNIQUE(') or upper.startswith('UNIQUE (') or \
           upper.startswith('CHECK') or upper.startswith('FOREIGN KEY'):
            continue

        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        col_name, col_def = parts[0], parts[1]
        col_def_upper = col_def.upper()

        if 'PRIMARY KEY' in col_def_upper or 'SERIAL' in col_def_upper:
            skipped.append(f"{table_name}.{col_name}")
            continue

        alter_statements.append(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_def};")

for stmt in alter_statements:
    print(stmt)

# Real gap found and fixed this session: this script previously only
# handled ALTER TABLE ADD COLUMN, never re-emitted the modparam
# catalog's own INSERT block -- meaning new catalog entries (like the
# lcr additions added alongside this fix) would never reach an
# already-installed Manager, only ever apply on a fresh install.
# Already idempotent (ON CONFLICT DO NOTHING), so safe to just
# extract and re-run verbatim on every reconcile pass -- same
# category of bug as the node-side upgrade-path gap found earlier
# this session, now fixed generally rather than as a one-off patch.
catalog_match = re.search(
    r'INSERT INTO platform_modparam_catalog.*?ON CONFLICT \(module, param_name\) DO NOTHING;',
    content, re.DOTALL
)
if catalog_match:
    print(catalog_match.group(0))
    catalog_rows = catalog_match.group(0).count("\n    ('")
else:
    catalog_rows = 0

print(f"-- {len(alter_statements)} reconciliation statements, {len(skipped)} structural columns skipped, "
      f"modparam catalog block {'found and re-emitted' if catalog_match else 'NOT FOUND -- check schema.sql format'}", file=sys.stderr)
