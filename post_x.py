#!/usr/bin/env python3
"""
Bitcoin Wizard -> X (Twitter) bot.

Posts a tweet every time a NEW wizard is inscribed on Bitcoin (a child of parent
inscription #70). Each tweet contains the wizard's ord.net link, which X
auto-unfurls into a large image card (the artwork + a link back to ord.net), so
no image upload is needed.

X keeps its OWN state marker (`lastXNumber`) — separate from the Discord bot's
marker — so the two advance independently. If X has a problem, Discord is
unaffected, and X simply retries its own missed wizards on the next run.

Modes (chosen via environment variables):
  DRY_RUN=1   Print the tweet that WOULD be sent (sample = latest wizard).
              No posting, no state change. Needs no credentials.
  X_TEST=1    Send ONE real test tweet of the latest wizard. No state change.
              (Requires X credentials.)
  (neither)   Live run: tweet every new wizard, advance lastXNumber, save state.
              (Requires X credentials.)

X credentials (OAuth 1.0a user context), via environment:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
  (the access token must have Read + Write permission)
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

# --- Configuration ------------------------------------------------------------

PARENT_ID = "b1c5baa2593b256068635bbc475e0cc439d66c2dcf12e9de6f3aaeaf96ff818bi0"
ORD = "https://ordinals.com"                 # reliable JSON source for detection
ORD_NET = "https://ord.net/inscription"      # human-facing link shown in tweets
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
USER_AGENT = "bitcoin-wizard-bot/1.0 (+https://www.bitcoinwizard.com)"

X_API_BASE = "https://api.twitter.com/2/tweets"

# --- HTTP helpers -------------------------------------------------------------

def get_json(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def fetch_all_children():
    children, page = [], 0
    while True:
        data = get_json(f"{ORD}/r/children/{PARENT_ID}/inscriptions/{page}")
        children.extend(data.get("children", []))
        if not data.get("more"):
            break
        page += 1
        if page > 200:
            break
    return children


# --- State --------------------------------------------------------------------

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


# --- OAuth 1.0a (no third-party libraries) ------------------------------------

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
    digest = hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _oauth_header(method, url, consumer_key, consumer_secret, access_token, token_secret):
    nonce = base64.b64encode(os.urandom(32)).decode().rstrip("=\n")
    ts = str(int(time.time()))
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


# --- X posting ----------------------------------------------------------------

def post_tweet(text, creds):
    consumer_key, consumer_secret, access_token, token_secret = creds
    payload = json.dumps({"text": text}).encode()
    auth = _oauth_header("POST", X_API_BASE, consumer_key, consumer_secret,
                         access_token, token_secret)
    req = urllib.request.Request(
        X_API_BASE,
        data=payload,
        headers={
            "Authorization": auth,
            "Content-Type":  "application/json",
            "User-Agent":    USER_AGENT,
        },
        method="POST",
    )
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:          # rate limited
                time.sleep(15)
            else:
                raise RuntimeError(f"X API error {e.code}: {body}")
        except Exception:  # noqa: BLE001
            time.sleep(2 * (i + 1))
    raise RuntimeError("Failed to post tweet after several attempts")


def build_tweet_text(idx, total, number):
    return (
        "✨ A new Bitcoin Wizard has been inscribed!\n"
        f"Wizard #{idx} of {total} — permanently on-chain ⚡\n\n"
        f"{ORD_NET}/{number}"
    )


def _get_creds():
    keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        print("ERROR: missing X credentials: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)
    # .strip() guards against a stray space/newline accidentally pasted into a
    # secret, which would otherwise break the OAuth signature -> 401.
    return tuple(os.environ[k].strip() for k in keys)


# --- Main ---------------------------------------------------------------------

def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    test_mode = os.environ.get("X_TEST") == "1"

    children = fetch_all_children()
    if not children:
        print("No children returned by the API; skipping this run.")
        return

    children.sort(key=lambda c: c["number"])
    total = len(children)
    index_of = {c["id"]: i + 1 for i, c in enumerate(children)}
    current_max = children[-1]["number"]

    # ---- preview / one-off test (uses the most recent wizard) ----
    if dry_run or test_mode:
        latest = children[-1]
        text = build_tweet_text(index_of[latest["id"]], total, latest["number"])
        if dry_run:
            print("[DRY RUN] would tweet:\n---\n" + text + "\n---")
            return
        post_tweet("\U0001f9ea Test — " + text, _get_creds())
        print("Test tweet sent. Check @bw_inscribe_bot.")
        return

    # ---- live run ----
    state = load_state()
    last_x = state.get("lastXNumber")

    if last_x is None:
        # First ever X run: set a baseline, tweet nothing (avoids 450+ tweets).
        print(f"First X run: baseline set at #{current_max}, tweeting nothing.")
        state["lastXNumber"] = current_max
        save_state(state)
        return

    new_ones = sorted((c for c in children if c["number"] > last_x),
                      key=lambda c: c["number"])
    if not new_ones:
        print(f"No new wizards for X (still {total}).")
        return

    creds = _get_creds()
    print(f"Tweeting {len(new_ones)} new wizard(s).")
    for c in new_ones:
        text = build_tweet_text(index_of[c["id"]], total, c["number"])
        try:
            post_tweet(text, creds)
            print(f"Tweeted Wizard #{index_of[c['id']]} (#{c['number']})")
            # Advance the marker after EACH success so a mid-batch failure never
            # re-tweets earlier ones; the next run resumes from here.
            state["lastXNumber"] = c["number"]
            save_state(state)
            time.sleep(3)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR tweeting #{c['number']}: {e}", file=sys.stderr)
            print("Stopping; next run retries from the last success.", file=sys.stderr)
            break

    print(f"Done. lastXNumber now {state.get('lastXNumber')}.")


if __name__ == "__main__":
    main()
