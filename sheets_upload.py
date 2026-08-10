#!/usr/bin/env python3
"""
Append rows from fixtures_import.csv to the Google Sheet Fixtures tab.

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
]


def get_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable not set")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# Columns that uniquely identify a fixture, used to skip rows already present
# in the sheet. status/event_id aren't part of the key since they're only
# populated later (by the Spond event-creation step), not by this script.
DEDUP_FIELDS = ("group_name", "date", "kick_off", "opponent")


def fixture_key(row: dict) -> tuple:
    return tuple(row.get(col, "").strip() for col in DEDUP_FIELDS)


DEDUP_COLUMN_INDICES = tuple(OUTPUT_FIELDS.index(col) for col in DEDUP_FIELDS)


def get_existing_keys(service) -> set:
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A2:{chr(ord('A') + len(OUTPUT_FIELDS) - 1)}",
    ).execute()
    keys = set()
    for row in resp.get("values", []):
        padded = row + [""] * (len(OUTPUT_FIELDS) - len(row))
        keys.add(tuple(padded[i].strip() for i in DEDUP_COLUMN_INDICES))
    return keys


def append_rows(service, rows: list[list]):
    body = {"values": rows}
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()


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

    existing_keys = get_existing_keys(service)
    new_rows = [row for row in input_rows if fixture_key(row) not in existing_keys]
    skipped = len(input_rows) - len(new_rows)
    if skipped:
        print(f"Skipping {skipped} row(s) already present in the sheet.", file=sys.stderr)

    if not new_rows:
        print("Nothing new to upload.", file=sys.stderr)
        return

    values = [[row.get(col, "") for col in OUTPUT_FIELDS] for row in new_rows]
    print(f"Appending {len(values)} row(s) to '{SHEET_TAB}' tab...", file=sys.stderr)
    append_rows(service, values)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
