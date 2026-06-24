#!/usr/bin/env python3
"""
Transform FA scraper CSV into Google Sheet fixture import format.

Usage:
    python transform.py [--input PATH] [--output PATH] [--config PATH]

If --input is omitted the script fetches the CSV from source_csv_url in config.json.
"""

import argparse
import csv
import json
import sys
import urllib.request
from datetime import datetime
from io import StringIO
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

OUTPUT_FIELDS = [
    "group_name",
    "date",
    "kick_off",
    "end_time",
    "meet_time_mins",
    "template",
    "opponent",
    "venue_name",
    "venue_address",
    "directions_url",
    "description",
    "rsvp_date",
    "send_date",
    "max_players",
    "auto_accept",
    "responses_admin_only",
    "comments_disabled",
    "banner_colour",
    "status",
    "banner_status",
    "event_id",
]


def load_config(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def fetch_csv(url: str) -> str:
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def normalise_date(raw: str) -> str:
    """Accept DD/MM/YYYY and pass through; try common alternatives."""
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return raw  # return as-is if unrecognised


def normalise_time(raw: str) -> str:
    """Accept HH:MM or H:MM and return HH:MM (24hr)."""
    raw = raw.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return raw


def derive_template(home_away: str, competition: str, overrides: dict) -> str:
    """Map home/away field and optionally competition keywords to template value."""
    comp_lower = competition.lower()
    for keyword, tmpl in overrides.items():
        if keyword in comp_lower and tmpl is not None:
            return tmpl

    ha = home_away.strip().upper()
    if ha in ("H", "HOME"):
        return "home"
    if ha in ("A", "AWAY"):
        return "away"
    return home_away.strip().lower()


def transform_row(row: dict, team_mapping: dict, defaults: dict, comp_overrides: dict) -> dict | None:
    team = row.get("team", "").strip()
    group_name = team_mapping.get(team)
    if group_name is None:
        # Try case-insensitive fallback
        team_lower = team.lower()
        for k, v in team_mapping.items():
            if k.lower() == team_lower:
                group_name = v
                break
    if group_name is None:
        print(f"WARNING: no group_name mapping for team '{team}' — row skipped", file=sys.stderr)
        return None

    template = derive_template(
        row.get("home_away", ""),
        row.get("competition", ""),
        comp_overrides,
    )

    return {
        "group_name": group_name,
        "date": normalise_date(row.get("date", "")),
        "kick_off": normalise_time(row.get("time", "")),
        "end_time": defaults["end_time"],
        "meet_time_mins": defaults["meet_time_mins"],
        "template": template,
        "opponent": row.get("opponent", "").strip(),
        "venue_name": row.get("venue", "").strip(),
        "venue_address": defaults["venue_address"],
        "directions_url": defaults["directions_url"],
        "description": defaults["description"],
        "rsvp_date": defaults["rsvp_date"],
        "send_date": defaults["send_date"],
        "max_players": defaults["max_players"],
        "auto_accept": defaults["auto_accept"],
        "responses_admin_only": defaults["responses_admin_only"],
        "comments_disabled": defaults["comments_disabled"],
        "banner_colour": defaults["banner_colour"],
        "status": "",
        "banner_status": "",
        "event_id": "",
    }


def main():
    parser = argparse.ArgumentParser(description="Transform FA scraper CSV to fixture import format")
    parser.add_argument("--input", help="Path to input CSV (omit to fetch from URL in config)")
    parser.add_argument("--output", default="fixtures_import.csv", help="Output CSV path")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.json"), help="Config JSON path")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    team_mapping = cfg["team_mapping"]
    defaults = cfg["defaults"]
    comp_overrides = cfg.get("competition_template_overrides", {})

    if args.input:
        raw = Path(args.input).read_text(encoding="utf-8")
    else:
        url = cfg["source_csv_url"]
        print(f"Fetching CSV from {url}", file=sys.stderr)
        raw = fetch_csv(url)

    reader = csv.DictReader(StringIO(raw))
    rows = list(reader)
    print(f"Read {len(rows)} input rows", file=sys.stderr)

    out_rows = []
    for row in rows:
        transformed = transform_row(row, team_mapping, defaults, comp_overrides)
        if transformed:
            out_rows.append(transformed)

    print(f"Writing {len(out_rows)} output rows to {args.output}", file=sys.stderr)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)

    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
