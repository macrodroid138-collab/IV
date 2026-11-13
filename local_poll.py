#!/usr/bin/env python3
"""
Keep SPY IV Pages data fresh from your PC (recommended over phone MacroDroid).

Default mode (best): run update_bars.py every 5 minutes and git push.
  - Uses your normal git login (GitHub Desktop / gh auth / SSH). No token in a file.

Optional --dispatch mode: ping GitHub Actions workflow_dispatch every 5 minutes.
  - Needs env var GITHUB_TOKEN (fine-grained: Actions Read/write on this repo).
  - Do NOT paste the token into this file.

Examples (from the repo folder on your PC):
  python local_poll.py
  python local_poll.py --once
  set GITHUB_TOKEN=ghp_...   && python local_poll.py --dispatch
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))
INTERVAL_SEC = 300
REPO = "macrodroid138-collab/IV"
WORKFLOW = "update-bars.yml"


def log(msg: str) -> None:
    print(f"[{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True)


def git_push_updates() -> None:
    run(["git", "pull", "--rebase", "origin", "main"], check=False)
    run([sys.executable, "update_bars.py"])

    paths = [
        "SPY_0dte_bars.json",
        "SPY_1dte_bars.json",
        "SPY_0dte_iv_ticks.csv",
        "SPY_1dte_iv_ticks.csv",
        ".iv_timestamps_et",
    ]
    existing = [p for p in paths if os.path.exists(os.path.join(ROOT, p))]
    if existing:
        run(["git", "add", *existing], check=False)
    # Ignore missing files in add
    staged = subprocess.run(
        ["git", "diff", "--staged", "--quiet"], cwd=ROOT
    ).returncode
    if staged == 0:
        log("No data changes to commit")
        return

    stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ")
    run(["git", "commit", "-m", f"Update SPY IV bars {stamp} (local)"])
    run(["git", "push", "origin", "HEAD:main"])
    log("Pushed to main")


def dispatch_workflow() -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit(
            "Set GITHUB_TOKEN in the environment (do not put it in this file).\n"
            "Fine-grained PAT: repo IV → Actions: Read and write"
        )
    url = (
        f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches"
    )
    body = b'{"ref":"main","inputs":{"loops":"1"}}'
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "IV-local-poll",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            log(f"Dispatched workflow HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Dispatch failed HTTP {e.code}: {detail}") from e


def main() -> None:
    ap = argparse.ArgumentParser(description="Poll SPY IV every 5 minutes from your PC")
    ap.add_argument(
        "--dispatch",
        action="store_true",
        help="Trigger GitHub Actions instead of running update_bars.py locally",
    )
    ap.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    ap.add_argument(
        "--interval",
        type=int,
        default=INTERVAL_SEC,
        help="Seconds between cycles (default 300)",
    )
    args = ap.parse_args()

    mode = "dispatch Actions" if args.dispatch else "local update + git push"
    log(f"Starting ({mode}), interval={args.interval}s")
    log("Ctrl+C to stop")

    while True:
        try:
            if args.dispatch:
                dispatch_workflow()
            else:
                git_push_updates()
        except subprocess.CalledProcessError as e:
            log(f"Command failed ({e.returncode}): {e}")
        except SystemExit as e:
            log(str(e))
            if args.once:
                raise
        except Exception as e:
            log(f"Error: {e}")

        if args.once:
            break
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
