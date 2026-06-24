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


def append_rows(service, rows: list[list]):
    body = {"values": rows}
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="USER_ENTERED",
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
        rows = [[row.get(col, "") for col in OUTPUT_FIELDS] for row in reader]

    if not rows:
        print("No rows to upload.", file=sys.stderr)
        return

    print(f"Appending {len(rows)} rows to '{SHEET_TAB}' tab...", file=sys.stderr)
    service = get_service()
    append_rows(service, rows)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
