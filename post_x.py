#!/usr/bin/env python3
"""
Bitcoin Wizard -> X (Twitter) bot.

Posts a tweet every time a NEW wizard is inscribed on Bitcoin.
Reuses the same state.json as the Discord bot so both bots stay
in sync with a single source of truth.

Environment variables (all required unless DRY_RUN=1):
  X_API_KEY             Consumer / API key
  X_API_SECRET          Consumer / API secret
  X_ACCESS_TOKEN        Access token  (must have Read+Write permission)
  X_ACCESS_TOKEN_SECRET Access token secret
  DRY_RUN=1             Fetch + report, but do not post.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

PARENT_ID = "b1c5baa2593b256068635bbc475e0cc439d66c2dcf12e9de6f3aaeaf96ff818bi0"
ORD       = "https://ordinals.com"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
USER_AGENT = "bitcoin-wizard-bot/1.0 (+https://www.bitcoinwizard.com)"
X_API_BASE = "https://api.twitter.com/2/tweets"

def get_json(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")

def fetch_all_children():
    children, page = [], 0
    while True:
        data = get_json(f"{ORD}/r/children/{PARENT_ID}/inscriptions/{page}")
        batch = data.get("children", [])
        children.extend(batch)
        if not data.get("more"):
            break
        page += 1
        if page > 200:
            break
    return children

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _percent_encode(s):
    return urllib.parse.quote(str(s), safe="")

def _oauth_signature(method, url, params, consumer_secret, token_secret):
    sorted_params = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}"
        for k, v in sorted(params.items())
    )
    base = "&".join([
        _percent_encode(method.upper()),
        _percent_encode(url),
        _percent_encode(sorted_params),
    ])
    signing_key = f"{_percent_encode(consumer_secret)}&{_percent_encode(token_secret)}"
    import hashlib, hmac, base64
    digest = hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()

def _oauth_header(method, url, consumer_key, consumer_secret, access_token, token_secret):
    import base64, os, time
    nonce = base64.b64encode(os.urandom(32)).decode().rstrip("=\n")
    ts    = str(int(time.time()))
    oauth_params = {
        "oauth_consumer_key":     consumer_key,
        "oauth_nonce":            nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        ts,
        "oauth_token":            access_token,
        "oauth_version":          "1.0",
    }
    sig = _oauth_signature(method, url, oauth_params, consumer_secret, token_secret)
    oauth_params["oauth_signature"] = sig
    return "OAuth " + ", ".join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )

def post_tweet(text, consumer_key, consumer_secret, access_token, token_secret):
    payload = json.dumps({"text": text}).encode()
    auth    = _oauth_header("POST", X_API_BASE, consumer_key, consumer_secret,
                            access_token, token_secret)
    req = urllib.request.Request(
        X_API_BASE, data=payload,
        headers={"Authorization": auth, "Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                time.sleep(15)
            else:
                raise RuntimeError(f"X API error {e.code}: {body}")
        except Exception:
            time.sleep(2 * (i + 1))
    raise RuntimeError("Failed to post tweet after several attempts")

def build_tweet(wizard_index, total, child):
    iid = child["id"]
    url = f"{ORD}/inscription/{iid}"
    return (
        f"\u2728 A new Bitcoin Wizard has been inscribed!\n\n"
        f"Wizard #{wizard_index} of {total} \u2014 now permanently on-chain.\n"
        f"Inscription #{child['number']:,}\n\n"
        f"{url}\n\n"
        f"#Bitcoin #Ordinals #BitcoinWizards"
    )

def main():
    dry_run         = os.environ.get("DRY_RUN") == "1"
    consumer_key    = os.environ.get("X_API_KEY", "")
    consumer_secret = os.environ.get("X_API_SECRET", "")
    access_token    = os.environ.get("X_ACCESS_TOKEN", "")
    token_secret    = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

    if not dry_run and not all([consumer_key, consumer_secret, access_token, token_secret]):
        print("ERROR: X API credentials are not fully set.", file=sys.stderr)
        sys.exit(1)

    state    = load_state()
    children = fetch_all_children()
    if not children:
        print("No children returned by the API; skipping.")
        return

    children.sort(key=lambda c: c["number"])
    total       = len(children)
    index_of    = {c["id"]: i + 1 for i, c in enumerate(children)}
    current_max = children[-1]["number"]
    last_max    = state.get("lastMaxNumber")

    if last_max is None:
        print(f"First run: baseline {total} wizards (max #{current_max}). Nothing posted.")
        return

    new_ones = sorted(
        (c for c in children if c["number"] > last_max),
        key=lambda c: c["number"],
    )

    if not new_ones:
        print(f"No new wizards (still {total}).")
        return

    print(f"Found {len(new_ones)} new wizard(s) - posting to X.")
    for c in new_ones:
        idx  = index_of[c["id"]]
        text = build_tweet(idx, total, c)
        if dry_run:
            print(f"[DRY RUN] would tweet:\n{text}\n")
        else:
            post_tweet(text, consumer_key, consumer_secret, access_token, token_secret)
            print(f"Tweeted Wizard #{idx} ({c['id']})")
            time.sleep(2)

    print("Done.")

if __name__ == "__main__":
    main()
