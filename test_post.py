#!/usr/bin/env python3
"""
Send ONE test card to your Discord channel so you can confirm the webhook
works, without waiting for a real new wizard. It posts the most recently
inscribed wizard, labelled as a test. Safe to run any time.
"""

import os
import sys

import check_wizards as w


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        sys.exit(1)

    children = w.fetch_all_children()
    if not children:
        print("Could not fetch any wizards.", file=sys.stderr)
        sys.exit(1)

    children.sort(key=lambda c: c["number"])
    total = len(children)
    latest = children[-1]
    embed = w.build_embed(total, total, latest)
    embed["title"] = "🧪 Test post — Bitcoin Wizard bot is working!"
    w.post_discord(webhook, embed)
    print("Test card sent. Check your Discord channel.")


if __name__ == "__main__":
    main()
