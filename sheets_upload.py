#!/usr/bin/env python3
"""
Sync rows from fixtures_import.csv into the Google Sheet Fixtures tab.

Each fixture is identified by FA's own per-fixture ID (captured by the
scraper from displayFixture.html?id=NNNN), which survives date/time/venue
changes on FA's site — unlike the (group, date, kick_off, opponent) key
alone. On each run:

  - A fixture_id not yet in the sheet is appended as a brand-new row.
  - A fixture_id already in the sheet whose date/kick_off/venue/opponent
    has changed has those columns synced to FA's current data and is
    flagged in review_flag/review_detail — but its Spond event, if any,
    is NOT auto-updated. A human reviews the flag and pushes the update
    to Spond via 05_apply_changes.py (in the PNFC repo) when ready.
  - A fixture_id that was in the sheet with an event_id (so a real Spond
    event exists) but is missing from today's scrape entirely is flagged
    as a possible cancellation/postponement, again without touching Spond.

Rows created before this fixture_id tracking existed are matched once by
the old (group, date, kick_off, opponent) key and backfilled with their
fixture_id, so they fold into the same tracking going forward.

Usage:
    python sheets_upload.py [--input PATH]

Requires env var GOOGLE_SERVICE_ACCOUNT_JSON containing the service account
key file contents (the full JSON string).
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = "14vuInvGzQnDMnUR2dg-Uk67kio83RBmdZG1_aQBiyno"
SHEET_TAB = "Fixtures"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

OUTPUT_FIELDS = [
    "group_name", "date", "kick_off", "end_time", "meet_time_mins",
    "template", "opponent", "venue_name", "venue_address", "directions_url",
    "description", "rsvp_date", "send_date", "max_players", "auto_accept",
    "responses_admin_only", "comments_disabled", "banner_colour",
    "status", "banner_status", "event_id",
    "fixture_id", "review_flag", "review_detail",
]

# The Fixtures tab has a header row (1) plus a hint/notes row (2); real data
# starts at row 3. Matches has_hint_row=True in 04_create_fixtures.py.
FIRST_DATA_ROW = 3

# Legacy key used to match rows created before fixture_id existed, so they
# can be backfilled instead of duplicated.
LEGACY_KEY_FIELDS = ("group_name", "date", "kick_off", "opponent")

# Fields whose change on an already-tracked fixture gets flagged for human
# review rather than applied automatically.
WATCHED_FIELDS = ("date", "kick_off", "venue_name", "opponent")

# Free-text fields compared case-insensitively — FA's site renders venue
# names in ALL CAPS while the sheet may have them in mixed case (manually
# entered, or from an older pipeline run), which isn't a real change.
CASE_INSENSITIVE_FIELDS = {"venue_name", "opponent"}


def get_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable not set")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def col_letter(n: int) -> str:
    """1-indexed column number -> spreadsheet column letter(s)."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def legacy_key(row: dict) -> tuple:
    return tuple(row.get(col, "").strip() for col in LEGACY_KEY_FIELDS)


def get_existing_rows(service) -> list[dict]:
    """Fetch existing sheet rows (below the header + hint rows) as dicts,
    each carrying its 1-indexed sheet row number under "_sheet_row"."""
    last_col = col_letter(len(OUTPUT_FIELDS))
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1:{last_col}",
    ).execute()
    values = resp.get("values", [])
    if len(values) < FIRST_DATA_ROW:
        return []
    header = values[0]
    rows = []
    for i, raw in enumerate(values[FIRST_DATA_ROW - 1:], start=FIRST_DATA_ROW):
        if not any(raw):
            continue
        padded = raw + [""] * (len(header) - len(raw))
        row = dict(zip(header, padded))
        row["_sheet_row"] = i
        rows.append(row)
    return rows


def append_rows(service, rows: list[list]):
    body = {"values": rows}
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()


def batch_write_cells(service, cell_updates: list[tuple]):
    """cell_updates: list of (sheet_row, column_name, value)."""
    if not cell_updates:
        return
    col_index = {name: i + 1 for i, name in enumerate(OUTPUT_FIELDS)}
    data = [
        {
            "range": f"{SHEET_TAB}!{col_letter(col_index[col_name])}{sheet_row}",
            "values": [[value]],
        }
        for sheet_row, col_name, value in cell_updates
    ]
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def diff_watched_fields(existing: dict, incoming: dict) -> list[str]:
    changes = []
    for field in WATCHED_FIELDS:
        old = existing.get(field, "").strip()
        new = incoming.get(field, "").strip()
        old_cmp, new_cmp = (old.upper(), new.upper()) if field in CASE_INSENSITIVE_FIELDS else (old, new)
        if old_cmp != new_cmp:
            changes.append(f"{field}: '{old}' → '{new}'")
    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="fixtures_import.csv")
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        input_rows = list(reader)

    if not input_rows:
        print("No rows to upload.", file=sys.stderr)
        return

    service = get_service()
    existing_rows = get_existing_rows(service)
    existing_by_fixture_id = {
        r["fixture_id"].strip(): r for r in existing_rows if r.get("fixture_id", "").strip()
    }
    existing_by_legacy_key = {
        legacy_key(r): r for r in existing_rows if not r.get("fixture_id", "").strip()
    }

    new_rows = []
    cell_updates = []
    seen_fixture_ids = set()
    changed_count = 0
    cleared_count = 0

    for row in input_rows:
        fid = row.get("fixture_id", "").strip()
        if fid:
            seen_fixture_ids.add(fid)

        existing = existing_by_fixture_id.get(fid) if fid else None
        matched_via_legacy = False
        if existing is None:
            existing = existing_by_legacy_key.get(legacy_key(row))
            matched_via_legacy = existing is not None

        if existing is None:
            new_rows.append(row)
            continue

        sheet_row = existing["_sheet_row"]
        if matched_via_legacy and fid:
            cell_updates.append((sheet_row, "fixture_id", fid))

        changes = diff_watched_fields(existing, row)
        if changes:
            detail_text = "; ".join(changes)
            # Keep the sheet's own data columns in sync with FA's current
            # data — review_flag/review_detail signal that Spond hasn't
            # caught up yet, not that the sheet itself is stale.
            for field in WATCHED_FIELDS:
                new_val = row.get(field, "").strip()
                if existing.get(field, "").strip() != new_val:
                    cell_updates.append((sheet_row, field, new_val))
            already_flagged = (existing.get("review_flag", "").strip() == "CHANGED"
                                and existing.get("review_detail", "").strip() == detail_text)
            if not already_flagged:
                cell_updates.append((sheet_row, "review_flag", "CHANGED"))
                cell_updates.append((sheet_row, "review_detail", detail_text))
                changed_count += 1
        elif existing.get("review_flag", "").strip():
            # Previously flagged (correctly or not) but no longer differs —
            # e.g. FA's data reverted, or a fixed comparison stops treating
            # it as a change. Clear the stale flag rather than leaving it
            # to mislead review forever.
            cell_updates.append((sheet_row, "review_flag", ""))
            cell_updates.append((sheet_row, "review_detail", ""))
            cleared_count += 1

    # Possible cancellations: sheet rows with a real Spond event that simply
    # weren't seen at all in today's scrape.
    cancelled_count = 0
    for r in existing_rows:
        fid = r.get("fixture_id", "").strip()
        if not fid or fid in seen_fixture_ids or not r.get("event_id", "").strip():
            continue
        flag_text = "CANCELLED?"
        if r.get("review_flag", "").strip() != flag_text:
            cell_updates.append((r["_sheet_row"], "review_flag", flag_text))
            cell_updates.append((r["_sheet_row"], "review_detail",
                                  "Missing from FA site — check for postponement/cancellation"))
            cancelled_count += 1

    if cell_updates:
        print(f"Updating {len(cell_updates)} cell(s): {changed_count} changed fixture(s) flagged, "
              f"{cancelled_count} possible cancellation(s) flagged, "
              f"{cleared_count} stale flag(s) cleared.", file=sys.stderr)
        batch_write_cells(service, cell_updates)

    if not new_rows:
        print("No new fixtures to upload.", file=sys.stderr)
        return

    values = [[row.get(col, "") for col in OUTPUT_FIELDS] for row in new_rows]
    print(f"Appending {len(values)} new row(s) to '{SHEET_TAB}' tab...", file=sys.stderr)
    append_rows(service, values)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
