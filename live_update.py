#!/usr/bin/env python3
"""
Tennis Live Update — 場中即時比分
從 Odds API /scores 拉取進行中比賽的即時比分，寫入 picks_latest.json
exit 0 = 有進行中比賽；exit 1 = 無任何進行中比賽（供迴圈計數用）
"""

import datetime
import json
import logging
import os
import sys
import time

import requests

log = logging.getLogger("tennis_live")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
JSON_PATH    = "docs/picks_latest.json"

TENNIS_SPORTS = [
    "tennis_atp", "tennis_wta",
    "tennis_atp_french_open",    "tennis_wta_french_open",
    "tennis_atp_wimbledon",      "tennis_wta_wimbledon",
    "tennis_atp_us_open",        "tennis_wta_us_open",
    "tennis_atp_australian_open","tennis_wta_australian_open",
    "tennis_atp_madrid_open",    "tennis_wta_madrid_open",
    "tennis_atp_rome",           "tennis_wta_rome",
]


def safe_get(url: str, params: dict = None):
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 404:
            log.debug("safe_get %s: 404 (sport inactive)", url.split("?")[0])
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("safe_get %s: %s", url.split("?")[0], e)
        return None


def fetch_live_scores() -> list:
    """
    從 /scores 端點抓進行中比賽。
    回傳 list of {home, away, home_score, away_score, sport_title, last_update}
    """
    if not ODDS_API_KEY:
        return []
    matches: list = []
    seen: set = set()
    for sport in TENNIS_SPORTS:
        data = safe_get(
            "https://api.the-odds-api.com/v4/sports/%s/scores/" % sport,
            params={"apiKey": ODDS_API_KEY, "daysFrom": 1},
        )
        if not data:
            continue
        for game in data:
            if game.get("completed"):
                continue
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            if not home or not away:
                continue
            key = "%s|%s" % (home.lower(), away.lower())
            if key in seen:
                continue
            seen.add(key)
            raw_scores = game.get("scores") or []
            home_score = next((s["score"] for s in raw_scores if s.get("name") == home), "")
            away_score = next((s["score"] for s in raw_scores if s.get("name") == away), "")
            matches.append({
                "home":        home,
                "away":        away,
                "home_score":  home_score,
                "away_score":  away_score,
                "sport_title": game.get("sport_title", ""),
                "last_update": game.get("last_update", ""),
            })
    log.info("Live scores: %d active matches", len(matches))
    return matches


def main() -> int:
    """
    回傳 0 = 有進行中比賽
    回傳 1 = 無進行中比賽（供外層 bash 迴圈計數）
    """
    if not os.path.exists(JSON_PATH):
        log.error("%s not found — run tennis_bot.py first", JSON_PATH)
        return 1

    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    live_matches = fetch_live_scores()

    now_tw = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    data["live_matches"]    = live_matches
    data["live_updated_at"] = now_tw.strftime("%Y-%m-%d %H:%M") + " (台灣時間)"
    data["live_updated_ts"] = int(time.time())

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    active = len(live_matches)
    log.info("Done — %d 場進行中比賽", active)
    return 0 if active > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
