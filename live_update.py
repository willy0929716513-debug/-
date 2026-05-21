#!/usr/bin/env python3
"""
Tennis Live Update — 場中即時比分 + 賠率監控
1. 從 Odds API /scores 拉取進行中比賽的即時比分
2. 從 Odds API /odds  拉取即時賠率，和賽前預測比較
3. 結果寫入 picks_latest.json
4. exit 0 = 有進行中比賽；exit 1 = 無任何進行中比賽（供迴圈計數用）
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
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "tennis-picks")
JSON_PATH    = "docs/picks_latest.json"

DRIFT_STRONG = 0.15   # >= 15% prob shift → 推播通知

TENNIS_SPORTS = [
    "tennis_atp", "tennis_wta",
    "tennis_atp_french_open",   "tennis_wta_french_open",
    "tennis_atp_wimbledon",     "tennis_wta_wimbledon",
    "tennis_atp_us_open",       "tennis_wta_us_open",
    "tennis_atp_australian_open","tennis_wta_australian_open",
    "tennis_atp_madrid_open",   "tennis_wta_madrid_open",
    "tennis_atp_rome",          "tennis_wta_rome",
]


def send_ntfy(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            "https://ntfy.sh",
            json={"topic": NTFY_TOPIC, "title": title,
                  "message": message, "priority": 4, "tags": ["tennis"]},
            timeout=10,
        )
    except Exception as e:
        log.warning("ntfy: %s", e)


def safe_get(url: str, params: dict = None):
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("safe_get %s: %s", url.split("?")[0], e)
        return None


def devigge(p1: float, p2: float) -> float:
    t = p1 + p2
    return p1 / t if t > 0 else 0.5


# ── 即時比分 ──────────────────────────────────────────────────────────────────

def fetch_live_scores() -> dict:
    """
    從 /scores 端點抓進行中比賽的即時比分。
    回傳 {matchup_key: {home, away, home_score, away_score, sport_title, last_update}}
    """
    if not ODDS_API_KEY:
        return {}
    scores: dict = {}
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
                continue                    # 跳過已結束比賽
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
            scores[key] = {
                "home":        home,
                "away":        away,
                "home_score":  home_score,
                "away_score":  away_score,
                "sport_title": game.get("sport_title", ""),
                "last_update": game.get("last_update", ""),
            }
    log.info("Live scores: %d active matches", len(scores))
    return scores


# ── 即時賠率 ──────────────────────────────────────────────────────────────────

def fetch_live_odds() -> dict:
    """抓即時賠率快照，回傳 {matchup_key: {...}}"""
    if not ODDS_API_KEY:
        return {}
    odds: dict = {}
    seen: set = set()
    for sport in TENNIS_SPORTS:
        data = safe_get(
            "https://api.the-odds-api.com/v4/sports/%s/odds/" % sport,
            params={"apiKey": ODDS_API_KEY, "regions": "us,eu",
                    "markets": "h2h", "oddsFormat": "decimal"},
        )
        if not data:
            continue
        for game in data:
            home  = game.get("home_team", "")
            away  = game.get("away_team", "")
            books = game.get("bookmakers", [])
            hp, ap = [], []
            for bk in books:
                for mkt in bk.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    for oc in mkt.get("outcomes", []):
                        pr = float(oc.get("price", 1.0))
                        if oc.get("name") == home:
                            hp.append(pr)
                        elif oc.get("name") == away:
                            ap.append(pr)
            if not hp or not ap:
                continue
            key = "%s|%s" % (home.lower(), away.lower())
            if key in seen:
                continue
            seen.add(key)
            cons_h = sum(hp) / len(hp)
            cons_a = sum(ap) / len(ap)
            dv_h   = devigge(1.0 / cons_h, 1.0 / cons_a)
            odds[key] = {
                "home": home, "away": away,
                "dv_p_home":  round(dv_h, 4),
                "best_home":  round(max(hp), 3),
                "best_away":  round(max(ap), 3),
            }
    log.info("Live odds: %d matches", len(odds))
    return odds


# ── 合併 ─────────────────────────────────────────────────────────────────────

def build_live_matches(scores: dict, odds: dict, game_preds: dict) -> list:
    """
    合併比分 + 賠率 + 賽前預測，產生 live_matches 列表。
    以 scores（進行中比賽）為主，odds 和 game_preds 補充資料。
    """
    result = []
    for key, sc in scores.items():
        od   = odds.get(key, {})
        pre  = game_preds.get(key, {})

        pre_p1  = float(pre.get("model_p1", 0.5)) if pre else None
        live_p1 = od.get("dv_p_home")
        drift   = round((live_p1 - pre_p1) * 100, 1) if (pre_p1 and live_p1) else None

        result.append({
            "home":        sc["home"],
            "away":        sc["away"],
            "home_score":  sc["home_score"],
            "away_score":  sc["away_score"],
            "sport_title": sc["sport_title"],
            "last_update": sc["last_update"],
            "best_home":   od.get("best_home"),
            "best_away":   od.get("best_away"),
            "drift":       drift,           # None if no pre-match data
        })
    return result


def check_drift_alerts(live_matches: list, prev_alerted: set) -> None:
    """推播賠率大幅漂移的新通知。"""
    for m in live_matches:
        d = m.get("drift")
        if d is None or abs(d) < DRIFT_STRONG * 100:
            continue
        key = "%s|%s" % (m["home"].lower(), m["away"].lower())
        if key in prev_alerted:
            continue
        winner = m["home"] if d > 0 else m["away"]
        send_ntfy(
            "🎾 場中信號 — %s 確立優勢" % winner,
            "%s vs %s\n賠率漂移 %+.0f%% | 比分 %s : %s" % (
                m["home"], m["away"], d,
                m["home_score"] or "—", m["away_score"] or "—",
            ),
        )
        prev_alerted.add(key)


# ── 主程式 ────────────────────────────────────────────────────────────────────

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

    game_preds = data.get("game_preds", {})
    prev_alerted = {
        "%s|%s" % (m["home"].lower(), m["away"].lower())
        for m in data.get("live_matches", [])
        if m.get("drift") is not None and abs(m.get("drift", 0)) >= DRIFT_STRONG * 100
    }

    scores       = fetch_live_scores()
    live_odds    = fetch_live_odds()
    live_matches = build_live_matches(scores, live_odds, game_preds)

    check_drift_alerts(live_matches, prev_alerted)

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
