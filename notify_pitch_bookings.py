#!/usr/bin/env python3
"""
Posts a Discord message when a new row appears in the "Tracker" tab of the
Pitch Booking Tracker Google Sheet (a Google Form response sheet —
https://docs.google.com/spreadsheets/d/1fZhm0pl1aqVOgXp9EeL4mlgFXjfIfEmlOGEHtyrEsH8),
or when an existing booking's Booking Status (column H) changes — e.g.
Submitted -> Confirmed -> Played. Nothing else changing on a row (Notes,
Confirmed with Vivacity, Invoice Reference, ...) triggers a notification.

A booking is identified by IDENTITY_FIELDS — Timestamp + Team + Date +
Start Time — which is what a form submission actually is and doesn't
change on a later manual edit.

Column lookup is by header NAME, not position, and only the first column
matching a given name is used. This matters because the sheet has been
seen to grow a second, blank, duplicate "Team/Group Making the Booking"
column (from an edited Form question) — naive `dict(zip(header, row))`
lets that later, blank duplicate silently overwrite the real team name
for every row, which broke the identity key for the entire tab in one go
and made every existing booking look "new" simultaneously. Keeping only
the first match sidesteps that regardless of where a duplicate lands.

State (the last-seen row per booking key) lives in
pitch_booking_state.json, committed back to the repo by the workflow after
each run — same pattern as fixtures_import.csv. A state file written by
the original, hash-only version of this script (a bare "seen_hashes"
list, with no "bookings" key) is treated as legacy: this run re-baselines
from it silently rather than posting every current row as "new".

On a first run (no state file yet), every row currently in the sheet is
recorded as already-seen WITHOUT posting to Discord — otherwise the very
first run would dump the entire existing tracker into the channel as if it
were all brand new.

Usage:
    python notify_pitch_bookings.py [--state pitch_booking_state.json]

Requires env var GOOGLE_SERVICE_ACCOUNT_JSON (service account key JSON, same
as sheets_upload.py) and DISCORD_WEBHOOK_URL (same secret notify_discord.py
uses — set it to a different Discord webhook if these should land in a
different channel).
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = "1fZhm0pl1aqVOgXp9EeL4mlgFXjfIfEmlOGEHtyrEsH8"
SHEET_TAB = "Tracker"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

TIMESTAMP_COL = "Timestamp"
TEAM_COL = "Team/Group Making the Booking"
DATE_COL = "Date of Requested Booking"
START_COL = "Start Time"
PITCH_COL = "Pitch Type Required"
STATUS_COL = "Booking Status (Default: Submitted)"
NOTES_COL = "Any additional Notes or requirements for this booking?"

# What identifies "the same booking" across runs — a form submission's
# Timestamp plus what it was actually booking. None of these are expected
# to change after submission (unlike Status, which is what this script
# watches for changes).
IDENTITY_FIELDS = (TIMESTAMP_COL, TEAM_COL, DATE_COL, START_COL)

MAX_LIST_ITEMS = 10


def get_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable not set")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def index_row(header: list[str], raw: list[str]) -> dict:
    """Zips a header row and a data row into a dict, keeping only the
    FIRST column for any header name that appears more than once (see
    module docstring — a later duplicate is not allowed to silently
    overwrite the real column's value)."""
    row = {}
    for i, name in enumerate(header):
        if name in row:
            continue
        row[name] = raw[i] if i < len(raw) else ""
    return row


def get_rows(service) -> list[dict]:
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}",
    ).execute()
    values = resp.get("values", [])
    if len(values) < 2:
        return []
    header = values[0]
    dupes = {name for name in header if header.count(name) > 1}
    if dupes:
        print(f"  ! WARNING: duplicate column header(s) in '{SHEET_TAB}': "
              f"{', '.join(sorted(dupes))} — using the first occurrence of each "
              f"and ignoring the rest.", file=sys.stderr)
    rows = []
    for raw in values[1:]:
        if not any(raw):
            continue
        rows.append(index_row(header, raw))
    return rows


def booking_key(row: dict) -> str:
    identity = [row.get(f, "").strip() for f in IDENTITY_FIELDS]
    canonical = json.dumps(identity, sort_keys=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"bookings": {}}
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"  ! WARNING: could not read state file {path} — starting fresh.", file=sys.stderr)
        return {"bookings": {}}
    if "bookings" not in state:
        # Legacy state from the hash-only version of this script — no
        # per-field snapshots to diff against, so treat this run as a
        # fresh baseline rather than guessing.
        print("  Legacy state file format detected — re-baselining without notifying.", file=sys.stderr)
        return {"bookings": {}, "_migrated": True}
    return state


def save_state(path: str, state: dict):
    state.pop("_migrated", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def booking_header(row: dict) -> str:
    team = row.get(TEAM_COL, "").strip() or "(unknown team)"
    date = row.get(DATE_COL, "").strip() or "(no date)"
    start = row.get(START_COL, "").strip() or "(no start time)"
    pitch = row.get(PITCH_COL, "").strip() or "(no pitch type)"
    return f"**{team}** — {date} at {start} — {pitch}"


def build_message(new_rows: list[dict], status_changes: list[tuple[dict, str, str]]) -> str:
    lines = []
    if new_rows:
        lines.append(f"**⚽ {len(new_rows)} new pitch booking(s) submitted**")
        for row in new_rows[:MAX_LIST_ITEMS]:
            status = row.get(STATUS_COL, "").strip() or "(no status)"
            lines.append(f"• {booking_header(row)} — {status}")
            notes = row.get(NOTES_COL, "").strip()
            if notes:
                lines.append(f"  {notes}")
        if len(new_rows) > MAX_LIST_ITEMS:
            lines.append(f"…and {len(new_rows) - MAX_LIST_ITEMS} more")

    if status_changes:
        if lines:
            lines.append("")
        lines.append(f"**🔄 {len(status_changes)} pitch booking(s) changed status**")
        for row, old_status, new_status in status_changes[:MAX_LIST_ITEMS]:
            lines.append(f"• {booking_header(row)} — {old_status or '(blank)'} → **{new_status or '(blank)'}**")
        if len(status_changes) > MAX_LIST_ITEMS:
            lines.append(f"…and {len(status_changes) - MAX_LIST_ITEMS} more")

    return "\n".join(lines)


def post_to_discord(webhook_url: str, content: str):
    # Discord message content is capped at 2000 chars.
    if len(content) > 2000:
        content = content[:1990] + "\n…(truncated)"
    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        # Discord's edge blocks urllib's default "Python-urllib/x.y" User-Agent
        # as bot-like, returning a bare 403 with no useful body — a real
        # browser/bot-style UA is required for the request to go through.
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/iaing80/Fixture_Merging, 1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord webhook returned HTTP {resp.status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="pitch_booking_state.json")
    args = parser.parse_args()

    service = get_service()
    rows = get_rows(service)
    print(f"Read {len(rows)} row(s) from '{SHEET_TAB}' tab.", file=sys.stderr)

    state = load_state(args.state)
    bookings = state.get("bookings", {})
    first_run = not bookings and not os.path.exists(args.state)
    silent_baseline = first_run or state.pop("_migrated", False)

    new_rows = []
    status_changes = []
    for row in rows:
        key = booking_key(row)
        previous = bookings.get(key)
        new_status = row.get(STATUS_COL, "").strip()
        if previous is None:
            bookings[key] = row
            if not silent_baseline:
                new_rows.append(row)
            continue
        old_status = previous.get(STATUS_COL, "").strip()
        if old_status != new_status:
            bookings[key] = row
            if not silent_baseline:
                status_changes.append((row, old_status, new_status))
        elif row != previous:
            # Some other field changed (Notes, Vivacity, Invoice, ...) —
            # keep the snapshot current so a LATER status change diffs
            # against the right prior state, but that's not itself worth
            # a Discord post.
            bookings[key] = row

    state["bookings"] = bookings
    save_state(args.state, state)

    if silent_baseline:
        print(f"Baselined {len(rows)} existing row(s), no notification sent.", file=sys.stderr)
        return

    if not new_rows and not status_changes:
        print("No new bookings or status changes.", file=sys.stderr)
        return

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(f"DISCORD_WEBHOOK_URL not set — skipping notification for "
              f"{len(new_rows)} new / {len(status_changes)} status change(s).", file=sys.stderr)
        return

    message = build_message(new_rows, status_changes)
    post_to_discord(webhook_url, message)
    print(f"Posted notification for {len(new_rows)} new / {len(status_changes)} status change(s) to Discord.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
