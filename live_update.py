#!/usr/bin/env python3
"""
Tennis Live Update — 場中賠率監控器
讀取 picks_latest.json → 比較即時賠率與賽前賠率的對比
→ 偵測背水路/逆轉機會 → 推播通知。
"""

import datetime
import json
import logging
import os
import time

import requests

log = logging.getLogger("tennis_live")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "tennis-picks")
JSON_PATH    = "docs/picks_latest.json"

# Odds drift thresholds
DRIFT_STRONG   = 0.15   # >= 15% prob shift → 強勢一方狀況明顯


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


def safe_get(url: str, params: dict = None) -> dict | None:
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("safe_get: %s", e)
        return None


def devigge(p1: float, p2: float) -> float:
    t = p1 + p2
    return p1 / t if t > 0 else 0.5


def fetch_live_odds() -> dict:
    """抓取即時賠率快照（The Odds API h2h）回傳 {matchup_key: dv_p_home}"""
    if not ODDS_API_KEY:
        return {}
    sports = ["tennis_atp", "tennis_wta",
              "tennis_atp_french_open", "tennis_wta_french_open",
              "tennis_atp_wimbledon", "tennis_wta_wimbledon"]
    live: dict = {}
    for sport in sports:
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
                        if oc["name"] == home:
                            hp.append(pr)
                        elif oc["name"] == away:
                            ap.append(pr)
            if not hp or not ap:
                continue
            cons_h = sum(hp) / len(hp)
            cons_a = sum(ap) / len(ap)
            dv_h   = devigge(1.0 / cons_h, 1.0 / cons_a)
            key = "%s|%s" % (home.lower(), away.lower())
            live[key] = {"home": home, "away": away, "dv_p_home": dv_h,
                         "best_home": max(hp), "best_away": max(ap)}
    log.info("Live odds: %d matches", len(live))
    return live


def generate_live_alerts(game_preds: dict, live_odds: dict) -> list:
    """Compare pre-match vs live odds to detect momentum shifts."""
    alerts = []
    for key, live in live_odds.items():
        pre = game_preds.get(key)
        if not pre:
            continue
        pre_p1   = float(pre.get("model_p1", 0.5))
        live_p1  = live["dv_p_home"]
        drift    = live_p1 - pre_p1

        bet = reason = None

        if drift >= DRIFT_STRONG:
            # Live market heavily favours p1 vs pre-match: p1 dominating
            bet    = "%s 獄站贏" % live["home"]
            reason = ("賠率智威大幅漂移 +%.0f%% → 場中確立優勢"
                      % (drift * 100))
        elif drift <= -DRIFT_STRONG:
            # p2 dominating
            bet    = "%s 獄站贏" % live["away"]
            reason = ("賠率智威大幅漂移 −%.0f%% → 逆轉流勢確立"
                      % (abs(drift) * 100))

        if bet:
            log.info("  LIVE ALERT: %s — %s", bet, reason)

        alerts.append({
            "home":       live["home"],
            "away":       live["away"],
            "pre_p1":     round(pre_p1 * 100, 1),
            "live_p1":    round(live_p1 * 100, 1),
            "drift":      round(drift * 100, 1),
            "best_home":  live["best_home"],
            "best_away":  live["best_away"],
            "bet":        bet,
            "reason":     reason,
        })

    return alerts


def main() -> None:
    if not os.path.exists(JSON_PATH):
        log.error("%s not found — run tennis_bot.py first", JSON_PATH)
        return

    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    game_preds = data.get("game_preds", {})
    log.info("game_preds: %d matches", len(game_preds))

    prev_alerts = {
        "%s|%s" % (a["home"].lower(), a["away"].lower())
        for a in data.get("live_matches", []) if a.get("bet")
    }

    live_odds  = fetch_live_odds()
    alerts     = generate_live_alerts(game_preds, live_odds)

    # Push notifications only for NEW alerts
    for a in alerts:
        if a.get("bet"):
            key = "%s|%s" % (a["home"].lower(), a["away"].lower())
            if key not in prev_alerts:
                send_ntfy(
                    "🎾 場中推薦 — %s" % a["bet"],
                    "%s vs %s\n%s" % (a["home"], a["away"], a["reason"])
                )

    now_tw = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    data["live_matches"]     = alerts
    data["live_updated_at"]  = now_tw.strftime("%Y-%m-%d %H:%M") + " (台灣時間)"
    data["live_updated_ts"]  = int(time.time())

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    bets = [a for a in alerts if a.get("bet")]
    log.info("Done — %d 場中比賽 / %d 個場中推薦", len(alerts), len(bets))


if __name__ == "__main__":
    main()
