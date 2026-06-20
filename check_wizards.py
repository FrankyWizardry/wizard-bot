 ##!/usr/bin/env python3
"""
Bitcoin Wizard -> Discord bot.

Posts a card to a Discord channel every time a NEW wizard is inscribed on
Bitcoin. Wizards are Ordinal inscriptions that are children of the parent
inscription #70 ("Magic Internet Money"), so "a new wizard" simply means
"a new child appeared under that parent".

Design goals: free, low-maintenance, low-bug.
  - No third-party libraries (uses only the Python standard library), so there
    is nothing to install and nothing that can break on a dependency update.
  - Full reconciliation each run (re-reads the whole list and compares against
    saved state) instead of relying on fragile ordering.
  - First run sets a baseline and posts nothing, so you never get spammed with
    the entire back catalogue.

Environment variables:
  DISCORD_WEBHOOK_URL  (required to post)  Your Discord channel webhook URL.
  DRY_RUN=1            (optional)          Fetch + report, but do not post.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --- Configuration ----------------------------------------------------------

PARENT_ID = "b1c5baa2593b256068635bbc475e0cc439d66c2dcf12e9de6f3aaeaf96ff818bi0"
ORD = "https://ordinals.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEARTBEAT_DAYS = 14          # commit a heartbeat at least this often (keeps the
                             # GitHub schedule from being auto-disabled)
EMBED_COLOR = 0xF7931A       # Bitcoin orange
USER_AGENT = "bitcoin-wizard-bot/1.0 (+https://www.bitcoinwizard.com)"

# --- Small HTTP helpers -----------------------------------------------------

def get_json(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - retry on anything transient
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def fetch_all_children():
    """Return every child inscription of the parent (paged)."""
    children, page = [], 0
    while True:
        data = get_json(f"{ORD}/r/children/{PARENT_ID}/inscriptions/{page}")
        batch = data.get("children", [])
        children.extend(batch)
        if not data.get("more"):
            break
        page += 1
        if page > 200:  # hard safety stop; collection is small
            break
    return children


# --- State ------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


# --- Discord ----------------------------------------------------------------

def build_embed(wizard_index, total, child, tweet_url=None):
    iid = child["id"]
    embed = {
        "title": f"â¨ A new Bitcoin Wizard has been inscribed!",
        "url": f"{ORD}/inscription/{iid}",
        "description": (
            f"**Wizard #{wizard_index}** of {total} â now permanently "
            f"on-chain.\nInscription #{child['number']:,}"
        ),
        "color": EMBED_COLOR,
        "image": {"url": f"{ORD}/content/{iid}"},
        "footer": {"text": "Bitcoin Wizard â¢ Magic Internet Money"},
    }
    # Optional link to the matching tweet (added by the X bot earlier in the run).
    # Absent/None -> card is exactly as before, so this can't affect anything.
    if tweet_url:
        embed["fields"] = [{"name": "🐦 On X", "value": tweet_url}]
    return embed


def post_discord(webhook, embed):
    payload = {"username": "Bitcoin Wizard", "embeds": [embed]}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    for i in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited - respect Discord's retry hint
                retry = 3.0
                try:
                    retry = float(json.load(e).get("retry_after", 3))
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(retry + 1)
            else:
                time.sleep(2 * (i + 1))
        except Exception:  # noqa: BLE001
            time.sleep(2 * (i + 1))
    raise RuntimeError("Failed to post to Discord after several attempts")


# --- Main -------------------------------------------------------------------

def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook and not dry_run:
        print("ERROR: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    children = fetch_all_children()
    if not children:
        print("No children returned by the API; skipping this run.")
        return

    # Sort by inscription number so position == order of inscription.
    children.sort(key=lambda c: c["number"])
    total = len(children)
    index_of = {c["id"]: i + 1 for i, c in enumerate(children)}
    current_max = children[-1]["number"]

    now = datetime.now(timezone.utc)
    last_max = state.get("lastMaxNumber")
    changed = False

    if last_max is None:
        # First ever run: establish a baseline, post nothing.
        print(f"First run: baseline set at {total} wizards (max #{current_max}).")
        state["lastMaxNumber"] = current_max
        state["lastHeartbeat"] = now.isoformat()
        changed = True
    else:
        new_ones = sorted(
            (c for c in children if c["number"] > last_max),
            key=lambda c: c["number"],
        )
        if new_ones:
            print(f"Found {len(new_ones)} new wizard(s).")
            # Tweet URLs the X bot recorded earlier this run (best-effort lookup).
            tweet_urls = state.get("tweetUrls") or {}
            if not isinstance(tweet_urls, dict):
                tweet_urls = {}
            for c in new_ones:
                idx = index_of[c["id"]]
                if dry_run:
                    print(f"[DRY RUN] would post Wizard #{idx} ({c['id']})")
                else:
                    tweet_url = tweet_urls.get(str(c["number"]))
                    post_discord(webhook, build_embed(idx, total, c, tweet_url=tweet_url))
                    print(f"Posted Wizard #{idx} ({c['id']})")
                    time.sleep(1.5)  # be gentle with Discord
            state["lastMaxNumber"] = current_max
            state["lastHeartbeat"] = now.isoformat()
            changed = True
        else:
            print(f"No new wizards (still {total}).")
            last_hb = state.get("lastHeartbeat")
            stale = True
            if last_hb:
                try:
                    stale = (now - datetime.fromisoformat(last_hb)).days >= HEARTBEAT_DAYS
                except Exception:  # noqa: BLE001
                    stale = True
            if stale:
                state["lastHeartbeat"] = now.isoformat()
                changed = True

    if changed and not dry_run:
        save_state(state)
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a") as f:
                f.write("state_changed=true\n")
    elif dry_run:
        print(f"[DRY RUN] total wizards now: {total}; current max #{current_max}")

    print("Done.")


if __name__ == "__main__":
    main()
!/usr/bin/env python3
"""
Bitcoin Wizard -> Discord bot.

Posts a card to a Discord channel every time a NEW wizard is inscribed on
Bitcoin. Wizards are Ordinal inscriptions that are children of the parent
inscription #70 ("Magic Internet Money"), so "a new wizard" simply means
"a new child appeared under that parent".

Design goals: free, low-maintenance, low-bug.
  - No third-party libraries (uses only the Python standard library), so there
    is nothing to install and nothing that can break on a dependency update.
  - Full reconciliation each run (re-reads the whole list and compares against
    saved state) instead of relying on fragile ordering.
  - First run sets a baseline and posts nothing, so you never get spammed with
    the entire back catalogue.

Environment variables:
  DISCORD_WEBHOOK_URL  (required to post)  Your Discord channel webhook URL.
  DRY_RUN=1            (optional)          Fetch + report, but do not post.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --- Configuration ----------------------------------------------------------

PARENT_ID = "b1c5baa2593b256068635bbc475e0cc439d66c2dcf12e9de6f3aaeaf96ff818bi0"
ORD = "https://ordinals.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEARTBEAT_DAYS = 14          # commit a heartbeat at least this often (keeps the
                             # GitHub schedule from being auto-disabled)
EMBED_COLOR = 0xF7931A       # Bitcoin orange
USER_AGENT = "bitcoin-wizard-bot/1.0 (+https://www.bitcoinwizard.com)"

# --- Small HTTP helpers -----------------------------------------------------

def get_json(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - retry on anything transient
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def fetch_all_children():
    """Return every child inscription of the parent (paged)."""
    children, page = [], 0
    while True:
        data = get_json(f"{ORD}/r/children/{PARENT_ID}/inscriptions/{page}")
        batch = data.get("children", [])
        children.extend(batch)
        if not data.get("more"):
            break
        page += 1
        if page > 200:  # hard safety stop; collection is small
            break
    return children


# --- State ------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


# --- Discord ----------------------------------------------------------------

def build_embed(wizard_index, total, child, tweet_url=None):
    iid = child["id"]
    embed = {
        "title": f"✨ A new Bitcoin Wizard has been inscribed!",
        "url": f"{ORD}/inscription/{iid}",
        "description": (
            f"**Wizard #{wizard_index}** of {total} — now permanently "
            f"on-chain.\nInscription #{child['number']:,}"
        ),
        "color": EMBED_COLOR,
        "image": {"url": f"{ORD}/content/{iid}"},
        "footer": {"text": "Bitcoin Wizard • Magic Internet Money"},
    }
    # Optional link to the matching tweet (added by the X bot earlier in the run).
    # Absent/None -> card is exactly as before, so this can't affect anything.
    if tweet_url:
        embed["fields"] = [{"name": "🐦 On X", "value": f"[View on X]({tweet_url})"}]
    return embed


def post_discord(webhook, embed):
    payload = {"username": "Bitcoin Wizard", "embeds": [embed]}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    for i in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited - respect Discord's retry hint
                retry = 3.0
                try:
                    retry = float(json.load(e).get("retry_after", 3))
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(retry + 1)
            else:
                time.sleep(2 * (i + 1))
        except Exception:  # noqa: BLE001
            time.sleep(2 * (i + 1))
    raise RuntimeError("Failed to post to Discord after several attempts")


# --- Main -------------------------------------------------------------------

def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook and not dry_run:
        print("ERROR: DISCORD_WEBHOOK_URL is not set.", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    children = fetch_all_children()
    if not children:
        print("No children returned by the API; skipping this run.")
        return

    # Sort by inscription number so position == order of inscription.
    children.sort(key=lambda c: c["number"])
    total = len(children)
    index_of = {c["id"]: i + 1 for i, c in enumerate(children)}
    current_max = children[-1]["number"]

    now = datetime.now(timezone.utc)
    last_max = state.get("lastMaxNumber")
    changed = False

    if last_max is None:
        # First ever run: establish a baseline, post nothing.
        print(f"First run: baseline set at {total} wizards (max #{current_max}).")
        state["lastMaxNumber"] = current_max
        state["lastHeartbeat"] = now.isoformat()
        changed = True
    else:
        new_ones = sorted(
            (c for c in children if c["number"] > last_max),
            key=lambda c: c["number"],
        )
        if new_ones:
            print(f"Found {len(new_ones)} new wizard(s).")
            # Tweet URLs the X bot recorded earlier this run (best-effort lookup).
            tweet_urls = state.get("tweetUrls") or {}
            if not isinstance(tweet_urls, dict):
                tweet_urls = {}
            for c in new_ones:
                idx = index_of[c["id"]]
                if dry_run:
                    print(f"[DRY RUN] would post Wizard #{idx} ({c['id']})")
                else:
                    tweet_url = tweet_urls.get(str(c["number"]))
                    post_discord(webhook, build_embed(idx, total, c, tweet_url=tweet_url))
                    print(f"Posted Wizard #{idx} ({c['id']})")
                    time.sleep(1.5)  # be gentle with Discord
            state["lastMaxNumber"] = current_max
            state["lastHeartbeat"] = now.isoformat()
            changed = True
        else:
            print(f"No new wizards (still {total}).")
            last_hb = state.get("lastHeartbeat")
            stale = True
            if last_hb:
                try:
                    stale = (now - datetime.fromisoformat(last_hb)).days >= HEARTBEAT_DAYS
                except Exception:  # noqa: BLE001
                    stale = True
            if stale:
                state["lastHeartbeat"] = now.isoformat()
                changed = True

    if changed and not dry_run:
        save_state(state)
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a") as f:
                f.write("state_changed=true\n")
    elif dry_run:
        print(f"[DRY RUN] total wizards now: {total}; current max #{current_max}")

    print("Done.")


if __name__ == "__main__":
    main()
