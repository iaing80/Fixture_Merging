#!/usr/bin/env python3
"""
Posts a Discord message for each new row that has appeared in the "Tracker"
tab of the Pitch Booking Tracker Google Sheet (a Google Form response sheet —
https://docs.google.com/spreadsheets/d/1fZhm0pl1aqVOgXp9EeL4mlgFXjfIfEmlOGEHtyrEsH8)
since the last run.

A row is identified by the tuple of every column in it — the sheet has no
dedicated ID column, but a form submission's Timestamp plus its other
answers is unique in practice, and hashing the whole row means a change to
any column (e.g. someone editing Pitch Type by hand) is picked up as a "new"
row too rather than silently ignored.

State (the set of row hashes already notified) lives in
pitch_booking_state.json, committed back to the repo by the workflow after
each run — same pattern as fixtures_import.csv.

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

# Columns we actually show in the Discord message, by header name as they
# appear in the sheet's own header row.
TEAM_COL = "Team/Group Making the Booking"
DATE_COL = "Date of Requested Booking"
START_COL = "Start Time"
PITCH_COL = "Pitch Type Required"
NOTES_COL = "Any additional Notes or requirements for this booking?"

MAX_LIST_ITEMS = 10


def get_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable not set")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_rows(service) -> list[dict]:
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}",
    ).execute()
    values = resp.get("values", [])
    if len(values) < 2:
        return []
    header = values[0]
    rows = []
    for raw in values[1:]:
        if not any(raw):
            continue
        padded = raw + [""] * (len(header) - len(raw))
        rows.append(dict(zip(header, padded)))
    return rows


def row_hash(row: dict) -> str:
    canonical = json.dumps(row, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"seen_hashes": []}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"  ! WARNING: could not read state file {path} — starting fresh.", file=sys.stderr)
        return {"seen_hashes": []}


def save_state(path: str, state: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def booking_line(row: dict) -> str:
    team = row.get(TEAM_COL, "").strip() or "(unknown team)"
    date = row.get(DATE_COL, "").strip() or "(no date)"
    start = row.get(START_COL, "").strip() or "(no start time)"
    pitch = row.get(PITCH_COL, "").strip() or "(no pitch type)"
    notes = row.get(NOTES_COL, "").strip()
    line = f"• **{team}** — {date} at {start} — {pitch}"
    if notes:
        line += f"\n  {notes}"
    return line


def build_message(new_rows: list[dict]) -> str:
    lines = [f"**⚽ {len(new_rows)} new pitch booking(s) submitted**"]
    for row in new_rows[:MAX_LIST_ITEMS]:
        lines.append(booking_line(row))
    if len(new_rows) > MAX_LIST_ITEMS:
        lines.append(f"…and {len(new_rows) - MAX_LIST_ITEMS} more")
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
    seen = set(state.get("seen_hashes", []))
    first_run = not os.path.exists(args.state)

    new_rows = []
    for row in rows:
        h = row_hash(row)
        if h not in seen:
            seen.add(h)
            if not first_run:
                new_rows.append(row)

    state["seen_hashes"] = list(seen)
    save_state(args.state, state)

    if first_run:
        print(f"First run — baselined {len(rows)} existing row(s), no notification sent.", file=sys.stderr)
        return

    if not new_rows:
        print("No new pitch bookings.", file=sys.stderr)
        return

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print(f"DISCORD_WEBHOOK_URL not set — skipping notification for "
              f"{len(new_rows)} new booking(s).", file=sys.stderr)
        return

    message = build_message(new_rows)
    post_to_discord(webhook_url, message)
    print(f"Posted notification for {len(new_rows)} new booking(s) to Discord.", file=sys.stderr)


if __name__ == "__main__":
    main()
