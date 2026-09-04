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
    to Spond via 17_apply_changes.py (in the PNFC repo) when ready.
  - A fixture_id that was in the sheet with an event_id (so a real Spond
    event exists) but is missing from today's scrape entirely is flagged
    as a possible cancellation/postponement, again without touching Spond.
  - A fixture_id still present in the scrape but carrying FA's own
    "Status / Notes" text (e.g. "Postponed" — FA leaves the original row in
    place rather than removing it) has that text synced to a dedicated
    fa_status column and is flagged for review, so it doesn't look like an
    ordinary unplayed fixture next to whatever replacement FA has scheduled.
    fa_status is deliberately separate from the sheet's existing "status"
    column, which the PNFC repo's scripts read as the Spond event's own
    lifecycle state (CREATED/FAILED/DELETED/...) — this script provisions
    fa_status itself (widening the sheet's grid and writing its header) the
    first time it's needed, since it postdates the sheet's original setup.

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
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SPREADSHEET_ID = "14vuInvGzQnDMnUR2dg-Uk67kio83RBmdZG1_aQBiyno"
SHEET_TAB = "Fixtures"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# A durable, append-only record of every watched-field change this script
# has ever detected (home/away reversals, date/kick-off/venue moves, ...) —
# separate from review_flag/review_detail on the Fixtures tab, which are
# cleared once a fixture's data stops differing so they only show the
# *current* state, not what changed and when.
CHANGE_LOG_TAB = "Change Log"
CHANGE_LOG_FIELDS = [
    "logged_at", "fixture_id", "group_name", "opponent", "date",
    "field", "old_value", "new_value",
]

# Google's own guidance for these APIs is to retry 429/5xx with backoff —
# they're transient (rate limiting, brief backend unavailability) and
# unrelated to anything this script sent. Without a retry, one blip on a
# scheduled run kills the whole sync (seen live: a bare 503 "service
# currently unavailable" from the very first read in ensure_fa_status_column
# aborted a run that had nothing else wrong with it).
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5


def execute_with_retry(request, max_retries: int = MAX_RETRIES):
    for attempt in range(max_retries + 1):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status if e.resp else None
            if status not in RETRYABLE_STATUS_CODES or attempt == max_retries:
                raise
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(f"  Sheets API returned {status}, retrying in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries})...", file=sys.stderr)
            time.sleep(delay)

OUTPUT_FIELDS = [
    "group_name", "date", "kick_off", "end_time", "meet_time_mins",
    "template", "opponent", "venue_name", "venue_address", "directions_url",
    "description", "rsvp_date", "send_date", "max_players", "auto_accept",
    "responses_admin_only", "comments_disabled", "banner_colour",
    "status", "banner_status", "event_id",
    "fixture_id", "review_flag", "review_detail", "fa_status", "confirm_cancel",
]

# The Fixtures tab has a header row (1) plus a hint/notes row (2); real data
# starts at row 3. Matches has_hint_row=True in 04_create_fixtures.py.
FIRST_DATA_ROW = 3

# Legacy key used to match rows created before fixture_id existed, so they
# can be backfilled instead of duplicated.
LEGACY_KEY_FIELDS = ("group_name", "date", "kick_off", "opponent")

# Fields whose change on an already-tracked fixture gets flagged for human
# review rather than applied automatically. "template" (home/away) is
# recomputed fresh from FA's data by transform.py's derive_template() on
# every run, same as the others here — it just wasn't being compared
# against the sheet's existing value, so a fixture that flipped home/away
# on FA's site never got its template cell corrected.
WATCHED_FIELDS = ("date", "kick_off", "venue_name", "opponent", "template")

# Free-text fields compared case-insensitively — FA's site renders venue
# names in ALL CAPS while the sheet may have them in mixed case (manually
# entered, or from an older pipeline run), which isn't a real change.
CASE_INSENSITIVE_FIELDS = {"venue_name", "opponent"}

# Friendlier field names for the Change Log tab — "template" is the sheet's
# internal column name for home/away, which isn't obvious out of context.
CHANGE_LOG_FIELD_LABELS = {"template": "home_away"}


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
    resp = execute_with_retry(service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1:{last_col}",
    ))
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
    execute_with_retry(service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ))


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
    execute_with_retry(service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ))


def diff_watched_fields(existing: dict, incoming: dict) -> list[str]:
    changes = []
    for field in WATCHED_FIELDS:
        old = existing.get(field, "").strip()
        new = incoming.get(field, "").strip()
        old_cmp, new_cmp = (old.upper(), new.upper()) if field in CASE_INSENSITIVE_FIELDS else (old, new)
        if old_cmp != new_cmp:
            changes.append(f"{field}: '{old}' → '{new}'")
    return changes


def fa_status_flag(fa_status: str) -> tuple[str, str]:
    """(review_flag, review_detail) for a non-empty FA status/notes value."""
    return fa_status.upper(), f"FA status: {fa_status} — check for Spond update"


def ensure_fa_status_column(service):
    """Make sure the Fixtures tab actually has an fa_status column before
    anything tries to write to it. fa_status was added to OUTPUT_FIELDS
    after 03_setup_sheet.py originally provisioned the sheet, so the grid
    may still be exactly the old width — the Sheets API rejects writes to
    cells beyond a sheet's current row/column count rather than silently
    growing it, so this has to run first. Idempotent — no-ops once the
    header is present."""
    header_resp = execute_with_retry(service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_TAB}!1:1",
    ))
    header = header_resp.get("values", [[]])
    header = header[0] if header else []
    if "fa_status" in header:
        return

    meta = execute_with_retry(service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID,
        ranges=[SHEET_TAB],
        fields="sheets(properties(sheetId,gridProperties))",
    ))
    sheets = meta.get("sheets", [])
    if not sheets:
        raise RuntimeError(f"Sheet tab '{SHEET_TAB}' not found")
    props = sheets[0]["properties"]
    sheet_id = props["sheetId"]
    current_cols = props["gridProperties"]["columnCount"]

    fa_status_col = OUTPUT_FIELDS.index("fa_status") + 1
    if current_cols < fa_status_col:
        execute_with_retry(service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{
                "appendDimension": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "length": fa_status_col - current_cols,
                },
            }]},
        ))

    col_ref = col_letter(fa_status_col)
    execute_with_retry(service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": f"{SHEET_TAB}!{col_ref}1", "values": [["fa_status"]]},
                {"range": f"{SHEET_TAB}!{col_ref}2",
                 "values": [["Leave blank — filled by sync script "
                             "(FA's own status text, e.g. Postponed)"]]},
            ],
        },
    ))


def ensure_change_log_tab(service):
    """Make sure the spreadsheet has a Change Log tab with its header row,
    creating it the first time this runs. Idempotent."""
    meta = execute_with_retry(service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID,
        fields="sheets(properties(title))",
    ))
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if CHANGE_LOG_TAB in titles:
        return

    execute_with_retry(service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": CHANGE_LOG_TAB}}}]},
    ))
    execute_with_retry(service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{CHANGE_LOG_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [CHANGE_LOG_FIELDS]},
    ))


def append_change_log(service, entries: list[dict]):
    """entries: list of dicts keyed by CHANGE_LOG_FIELDS. Appends one row
    per entry to the Change Log tab — never edited or cleared afterwards,
    so it stays a durable record of every change ever detected."""
    if not entries:
        return
    values = [[e.get(col, "") for col in CHANGE_LOG_FIELDS] for e in entries]
    execute_with_retry(service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{CHANGE_LOG_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ))


def parse_sheet_date(raw: str):
    """Parse the sheet's date column (DD/MM/YYYY, per transform.py's
    normalise_date) into a date object, or None if unparseable/blank —
    callers treat None as "can't tell, don't assume it's past"."""
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# Amber (Material Design amber 500, #FFC107) used to highlight any row
# awaiting human review, whatever put it there (CHANGED, CANCELLED?, an FA
# status flag like POSTPONED, ...).
REVIEW_HIGHLIGHT_COLOR = {"red": 1.0, "green": 0.757, "blue": 0.027}


def ensure_review_flag_highlighting(service):
    """Make sure a conditional format rule exists that highlights a data row
    amber whenever its review_flag cell is non-empty, so fixtures awaiting
    review stand out in the sheet itself rather than only in review_flag's
    raw text. Idempotent — checks for an existing matching rule first, since
    this runs on every sheets_upload.py invocation."""
    review_flag_col = col_letter(OUTPUT_FIELDS.index("review_flag") + 1)
    formula = f"=${review_flag_col}{FIRST_DATA_ROW}<>\"\""

    meta = execute_with_retry(service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID,
        ranges=[SHEET_TAB],
        fields="sheets(properties(sheetId,title),conditionalFormats)",
    ))
    sheets = meta.get("sheets", [])
    if not sheets:
        raise RuntimeError(f"Sheet tab '{SHEET_TAB}' not found")
    sheet = sheets[0]
    sheet_id = sheet["properties"]["sheetId"]

    for fmt in sheet.get("conditionalFormats", []):
        rule = fmt.get("booleanRule", {})
        values = rule.get("condition", {}).get("values", [])
        if (rule.get("condition", {}).get("type") == "CUSTOM_FORMULA"
                and values and values[0].get("userEnteredValue") == formula):
            return  # already set up

    request = {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": FIRST_DATA_ROW - 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(OUTPUT_FIELDS),
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": formula}],
                    },
                    "format": {"backgroundColor": REVIEW_HIGHLIGHT_COLOR},
                },
            },
            "index": 0,
        }
    }
    execute_with_retry(service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={"requests": [request]}
    ))


def fixture_heading(row: dict) -> str:
    group = row.get("group_name", "").strip()
    opponent = row.get("opponent", "").strip()
    date = row.get("date", "").strip()
    kick_off = row.get("kick_off", "").strip()
    heading = f"{group} vs {opponent}".strip(" vs")
    return f"{heading} ({date} {kick_off})".strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="fixtures_import.csv")
    parser.add_argument("--notify-summary", default="notify_summary.json",
                         help="Path to write a JSON summary of new/changed/cancelled "
                              "fixtures for a downstream notification step.")
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
        write_notify_summary(args.notify_summary, [], [], [], [])
        return

    service = get_service()
    ensure_fa_status_column(service)
    ensure_review_flag_highlighting(service)
    ensure_change_log_tab(service)
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
    fa_status_count = 0
    changed_summary = []
    cancelled_summary = []
    fa_status_summary = []
    change_log_entries = []
    logged_at = datetime.now().isoformat(timespec="seconds")

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

        fa_status_new = row.get("fa_status", "").strip()
        fa_status_old = existing.get("fa_status", "").strip()
        if fa_status_new != fa_status_old:
            cell_updates.append((sheet_row, "fa_status", fa_status_new))

        if fa_status_new:
            # A confirm_cancel=TRUE row has already had its Spond event
            # cancelled by 17_apply_changes.py (in PNFC) — that's a durable
            # marker, not a one-shot toggle, precisely so a fixture FA still
            # lists as e.g. "Postponed" on every later scrape doesn't keep
            # reopening it for review each run.
            if existing.get("confirm_cancel", "").strip().upper() == "TRUE":
                continue

            # FA's own status/notes text (e.g. "Postponed") takes priority
            # over the watched-field diff below — a postponed fixture keeps
            # its original date/venue on FA's site, so there's usually
            # nothing else to diff, and this is the more useful signal.
            flag_text, detail_text = fa_status_flag(fa_status_new)
            already_flagged = (existing.get("review_flag", "").strip() == flag_text
                                and existing.get("review_detail", "").strip() == detail_text)
            if not already_flagged:
                cell_updates.append((sheet_row, "review_flag", flag_text))
                cell_updates.append((sheet_row, "review_detail", detail_text))
                fa_status_count += 1
                fa_status_summary.append({"heading": fixture_heading(row), "detail": detail_text})
            continue

        changes = diff_watched_fields(existing, row)
        if changes:
            detail_text = "; ".join(changes)
            # Keep the sheet's own data columns in sync with FA's current
            # data — review_flag/review_detail signal that Spond hasn't
            # caught up yet, not that the sheet itself is stale.
            for field in WATCHED_FIELDS:
                old_val = existing.get(field, "").strip()
                new_val = row.get(field, "").strip()
                if old_val != new_val:
                    cell_updates.append((sheet_row, field, new_val))
                    change_log_entries.append({
                        "logged_at": logged_at,
                        "fixture_id": fid,
                        "group_name": row.get("group_name", "").strip(),
                        "opponent": row.get("opponent", "").strip(),
                        "date": row.get("date", "").strip(),
                        "field": CHANGE_LOG_FIELD_LABELS.get(field, field),
                        "old_value": old_val,
                        "new_value": new_val,
                    })
                    if field == "venue_name":
                        # venue_address is hand-maintained against the old
                        # venue_name — it no longer describes the new venue,
                        # so blank it rather than leave a stale address paired
                        # with the new name.
                        cell_updates.append((sheet_row, "venue_address", ""))
            already_flagged = (existing.get("review_flag", "").strip() == "CHANGED"
                                and existing.get("review_detail", "").strip() == detail_text)
            if not already_flagged:
                cell_updates.append((sheet_row, "review_flag", "CHANGED"))
                cell_updates.append((sheet_row, "review_detail", detail_text))
                changed_count += 1
                changed_summary.append({"heading": fixture_heading(row), "detail": detail_text})
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
    today = datetime.now().date()
    cancelled_count = 0
    for r in existing_rows:
        fid = r.get("fixture_id", "").strip()
        if not fid or fid in seen_fixture_ids or not r.get("event_id", "").strip():
            continue

        # A fixture already played typically drops off FA's site entirely
        # rather than staying listed with a result — that's normal housekeeping
        # on FA's end, not a cancellation, so a past date is never treated as
        # "missing from the scrape" grounds for CANCELLED?. An unparseable date
        # is treated as not-past (fail safe towards flagging, same as before).
        fixture_date = parse_sheet_date(r.get("date", ""))
        if fixture_date is not None and fixture_date < today:
            continue

        flag_text = "CANCELLED?"
        if r.get("review_flag", "").strip() != flag_text:
            cell_updates.append((r["_sheet_row"], "review_flag", flag_text))
            cell_updates.append((r["_sheet_row"], "review_detail",
                                  "Missing from FA site — check for postponement/cancellation"))
            cancelled_count += 1
            cancelled_summary.append({"heading": fixture_heading(r)})

    if cell_updates:
        print(f"Updating {len(cell_updates)} cell(s): {changed_count} changed fixture(s) flagged, "
              f"{fa_status_count} FA status fixture(s) flagged, "
              f"{cancelled_count} possible cancellation(s) flagged, "
              f"{cleared_count} stale flag(s) cleared.", file=sys.stderr)
        batch_write_cells(service, cell_updates)

    if change_log_entries:
        print(f"Appending {len(change_log_entries)} row(s) to '{CHANGE_LOG_TAB}' tab...", file=sys.stderr)
        append_change_log(service, change_log_entries)

    # A brand-new fixture can arrive already carrying an FA status (e.g. a
    # rearranged fixture picked up for the first time still showing old
    # notes) — flag it for review immediately rather than waiting for a
    # later run's diff to notice.
    for row in new_rows:
        fa_status_new = row.get("fa_status", "").strip()
        if fa_status_new:
            row["review_flag"], row["review_detail"] = fa_status_flag(fa_status_new)
            fa_status_summary.append({"heading": fixture_heading(row), "detail": row["review_detail"]})

    new_summary = [{"heading": fixture_heading(row)} for row in new_rows]

    if new_rows:
        values = [[row.get(col, "") for col in OUTPUT_FIELDS] for row in new_rows]
        print(f"Appending {len(values)} new row(s) to '{SHEET_TAB}' tab...", file=sys.stderr)
        append_rows(service, values)
        print("Done.", file=sys.stderr)
    else:
        print("No new fixtures to upload.", file=sys.stderr)

    write_notify_summary(args.notify_summary, new_summary, changed_summary, cancelled_summary, fa_status_summary)


def write_notify_summary(path, new_summary, changed_summary, cancelled_summary, fa_status_summary):
    """Write a JSON summary of this run's new/changed/cancelled/FA-status fixtures
    for a downstream notification step (e.g. a Discord webhook in the workflow)
    to read — kept separate from the sheet writes above so notification delivery
    can fail or be skipped without affecting the sheet itself."""
    summary = {
        "new_fixtures": new_summary,
        "changed_fixtures": changed_summary,
        "cancelled_fixtures": cancelled_summary,
        "fa_status_fixtures": fa_status_summary,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
