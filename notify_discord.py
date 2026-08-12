#!/usr/bin/env python3
"""
Posts a Discord message summarising new/changed/cancelled fixtures from a
sheets_upload.py --notify-summary JSON file. Does nothing (exit 0) if the
summary is empty, so it's safe to always run this step after sheets_upload.py.

Usage:
    python notify_discord.py --summary notify_summary.json

Requires env var DISCORD_WEBHOOK_URL.
"""

import argparse
import json
import os
import sys
import urllib.request

MAX_LIST_ITEMS = 10


def build_message(summary: dict) -> str | None:
    new = summary.get("new_fixtures", [])
    changed = summary.get("changed_fixtures", [])
    cancelled = summary.get("cancelled_fixtures", [])

    if not (new or changed or cancelled):
        return None

    lines = ["**Fixture sync update**"]

    if new:
        lines.append(f"\n🆕 **{len(new)} new fixture(s)**")
        for f in new[:MAX_LIST_ITEMS]:
            lines.append(f"• {f['heading']}")
        if len(new) > MAX_LIST_ITEMS:
            lines.append(f"…and {len(new) - MAX_LIST_ITEMS} more")

    if changed:
        lines.append(f"\n✏️ **{len(changed)} fixture(s) changed** — review before applying to Spond")
        for f in changed[:MAX_LIST_ITEMS]:
            lines.append(f"• {f['heading']}\n  {f['detail']}")
        if len(changed) > MAX_LIST_ITEMS:
            lines.append(f"…and {len(changed) - MAX_LIST_ITEMS} more")

    if cancelled:
        lines.append(f"\n⚠️ **{len(cancelled)} possible cancellation(s)** — check for postponement/cancellation")
        for f in cancelled[:MAX_LIST_ITEMS]:
            lines.append(f"• {f['heading']}")
        if len(cancelled) > MAX_LIST_ITEMS:
            lines.append(f"…and {len(cancelled) - MAX_LIST_ITEMS} more")

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
    parser.add_argument("--summary", default="notify_summary.json")
    args = parser.parse_args()

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL not set — skipping notification.", file=sys.stderr)
        return

    with open(args.summary, encoding="utf-8") as f:
        summary = json.load(f)

    message = build_message(summary)
    if message is None:
        # Still post a heartbeat rather than staying silent — silence is
        # ambiguous (ran clean vs. never ran vs. crashed before this step).
        message = "✅ Fixture sync ran — no new, changed, or cancelled fixtures."
        print("No new/changed/cancelled fixtures — posting heartbeat.", file=sys.stderr)

    post_to_discord(webhook_url, message)
    print("Posted notification to Discord.", file=sys.stderr)


if __name__ == "__main__":
    main()
