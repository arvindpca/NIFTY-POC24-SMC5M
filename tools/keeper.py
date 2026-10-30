#!/usr/bin/env python3
"""KEEPER - paper bot ko zinda rakhta hai, GitHub cron par bharosa kiye bina.

* Har 5 min check: NSE time (Mon-Fri 09:16-15:10 IST) me paper-trading chal raha hai ya line me hai?
  Nahi -> turant restart (workflow_dispatch) + Telegram par bata do.
* Ek run ~5h40m chalta hai, phir apna agla run khud chala deta hai (self-dispatch),
  isliye chain cron ke bina bhi chalti rehti hai. Cron sirf backup hai.
* Agar bot baar-baar turant toot raha ho (din me MAX_RESTARTS se zyada), restart rok kar alert.
Paper only - koi order nahi.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
PAPER_WF = "paper-trading.yml"
SELF_WF = "keeper.yml"
CHECK_EVERY_SEC = 300
RUN_BUDGET_MIN = float(os.getenv("KEEPER_BUDGET_MIN", "340"))   # GitHub limit 360
WATCH_FROM, WATCH_TO = dtime(9, 16), dtime(15, 10)
MAX_RESTARTS = 8
ALIVE = {"in_progress", "queued", "pending", "waiting", "requested"}


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "User-Agent": "keeper", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def dispatch(wf):
    try:
        api(f"actions/workflows/{wf}/dispatches", "POST", {"ref": "main"})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"dispatch {wf} failed: {e}")
        return False


def telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", method="POST",
            data=json.dumps({"chat_id": chat, "text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print(f"telegram failed: {e}")


def paper_alive():
    for status in ("in_progress", "queued", "pending", "waiting", "requested"):
        runs = api(f"actions/workflows/{PAPER_WF}/runs?status={status}&per_page=1")
        if runs.get("total_count", 0) > 0:
            return True
    return False


def in_watch_window(now):
    return now.weekday() < 5 and WATCH_FROM <= now.time() <= WATCH_TO


def main():
    start = time.time()
    restarts = {}           # date -> count
    gave_up = set()
    print(f"Keeper started {datetime.now(IST):%d-%b %H:%M} IST, budget {RUN_BUDGET_MIN:.0f} min")
    while True:
        now = datetime.now(IST)
        if in_watch_window(now):
            try:
                alive = paper_alive()
            except Exception as e:  # noqa: BLE001  (GitHub API glitch: try again next round)
                print(f"{now:%H:%M} status check failed: {e}")
                alive = True
            day = now.date().isoformat()
            if alive:
                print(f"{now:%H:%M} paper bot OK")
            elif restarts.get(day, 0) >= MAX_RESTARTS:
                if day not in gave_up:
                    gave_up.add(day)
                    telegram(f"🚨 KEEPER - NIFTY-POC24-SMC5M\nPaper bot aaj {MAX_RESTARTS} baar restart "
                             f"karne par bhi ruk ja raha hai. Ab restart band, code/log check karna hoga.\n"
                             f"https://github.com/{REPO}/actions/workflows/{PAPER_WF}")
            else:
                ok = dispatch(PAPER_WF)
                restarts[day] = restarts.get(day, 0) + 1
                print(f"{now:%H:%M} paper bot NOT running -> restart {'OK' if ok else 'FAILED'}")
                telegram(f"🔄 KEEPER - NIFTY-POC24-SMC5M\nPaper bot band mila ({now:%H:%M} IST), "
                         f"{'khud restart kar diya' if ok else 'restart FAIL hua, check karo'}.\n"
                         f"Aaj ka restart #{restarts[day]}")
        used_min = (time.time() - start) / 60
        if used_min + CHECK_EVERY_SEC / 60 >= RUN_BUDGET_MIN:
            ok = dispatch(SELF_WF)
            print(f"Budget used ({used_min:.0f} min). Next keeper {'queued' if ok else 'FAILED to queue'}.")
            if not ok:
                telegram("⚠️ KEEPER agla run start nahi kar paya. Backup cron 20 min me dobara chalu karega.")
            return 0
        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
