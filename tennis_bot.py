#!/usr/bin/env python3
"""
Tennis Bot v3.2 — ATP/WTA 巡迴賽預測系統
9因子模型：Surface ELO 25% + Markov Chain 25% + Hold/Break 20% + Advanced Stats 30%
附加調整：體能(年齡加權) ±10% | 場地狀態 ±5% | H2H ±5% | 搶七/關鍵分 ±7%
         雙誤懲罰 ±4% | 左手剋制 ±3% | 反拍剋制 ±2% | 室內場速(進入發球模型)
資料來源：Jeff Sackmann ATP/WTA CSVs + The Odds API
"""

import csv
import datetime
import io
import json
import logging
import math
import os
import random
import time
from typing import Dict, List, Optional, Tuple

import requests

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
log = logging.getLogger("tennis_bot")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
ODDS_API_KEY  = os.environ.get("ODDS_API_KEY", "")
NTFY_TOPIC    = os.environ.get("NTFY_TOPIC", "tennis-picks")
DISCORD_HOOK  = os.environ.get("DISCORD_WEBHOOK", "")
GIST_TOKEN    = os.environ.get("GIST_TOKEN", "")
GIST_ID       = os.environ.get("GIST_ID", "")

JSON_PATH     = "docs/picks_latest.json"

KELLY         = 0.25
KELLY_MAX     = 200.0
KELLY_FLOOR   = 50.0
BANKROLL      = 1000.0
MAX_DAILY_EXP = 500.0

MIN_EDGE_ML   = 0.06
MIN_CONF_ML   = 0.60
MIN_BOOKS     = 3
MAX_PICKS     = 6

# ─────────────────────────────────────────────────────────────────────────────
# ADVANCED MODEL CONSTANTS  (v3)
# ─────────────────────────────────────────────────────────────────────────────
AGE_FATIGUE_SCALE = 0.06    # extra fatigue multiplier per year above 28
LEFTY_SERVE_BONUS = 0.012   # serve point adj for lefty serving vs righty
LEFTY_GRASS_EXTRA = 0.006   # additional grass lefty bonus
BH_TOPSPIN_VULN   = 0.008   # 1h backhand vulnerability vs lefty topspin (clay)

INDOOR_TOURNAMENTS = {
    "paris", "rotterdam", "vienna", "sofia", "marseille",
    "montpellier", "dallas", "memphis", "zhuhai", "moscow",
    "basel", "cologne", "st_petersburg", "astana", "nur-sultan",
    "bercy", "indoor",
}

COURT_SPEED_ADJ: Dict[str, float] = {
    # Indoor hard (fast)
    "paris":            +0.018,
    "rotterdam":        +0.020,
    "vienna":           +0.018,
    "sofia":            +0.016,
    "marseille":        +0.016,
    "dallas":           +0.014,
    # Outdoor hard variations
    "us_open":          +0.010,
    "australian_open":  +0.008,
    "miami":            +0.005,
    "indian_wells":     +0.005,
    # Slow clay
    "monte_carlo":      -0.005,
    "hamburg":          -0.003,
    # Fast grass
    "halle":            +0.006,
    "queens":           +0.006,
    "eastbourne":       +0.004,
}

# ─────────────────────────────────────────────────────────────────────────────
# ALTITUDE  (metres above sea level for tournament cities)
# ─────────────────────────────────────────────────────────────────────────────
ALTITUDE_M: Dict[str, int] = {
    "buenos_aires": 1138, "bogota": 2600, "quito": 2850, "lima": 154,
    "santiago": 520, "mexico_city": 2250, "guadalajara": 1566,
    "madrid": 667, "kitzbuhel": 762, "gstaad": 1060,
    "granada": 685, "lyon": 173, "chengdu": 506, "kunming": 1895,
}

# ─────────────────────────────────────────────────────────────────────────────
# TOURNAMENT COORDINATES  (lat, lon) for weather fetch
# ─────────────────────────────────────────────────────────────────────────────
TOURNAMENT_COORDS: Dict[str, Tuple[float, float]] = {
    "roland_garros":    (48.847,   2.250),
    "french_open":      (48.847,   2.250),
    "wimbledon":        (51.434,  -0.214),
    "us_open":          (40.750, -73.846),
    "australian_open":  (-37.821, 144.981),
    "indian_wells":     (33.720, -116.369),
    "miami":            (25.683,  -80.180),
    "monte_carlo":      (43.745,   7.427),
    "madrid":           (40.416,  -3.703),
    "rome":             (41.897,  12.469),
    "barcelona":        (41.389,   2.165),
    "halle":            (51.932,   8.660),
    "queens":           (51.490,  -0.212),
    "eastbourne":       (50.768,   0.280),
    "hamburg":          (53.553,   9.992),
    "toronto":          (43.641,  -79.382),
    "cincinnati":       (39.104,  -84.510),
    "beijing":          (39.906, 116.391),
    "shanghai":         (31.225, 121.474),
    "kitzbuhel":        (47.444,  12.391),
    "buenos_aires":     (-34.614, -58.382),
    "rio":              (-22.906, -43.172),
    "bogota":           ( 4.711,  -74.072),
    "santiago":         (-33.437, -70.650),
    "umag":             (45.434,  13.524),
    "bastad":           (56.430,  12.854),
    "gstaad":           (46.474,   7.288),
}

ODDS_PREV_PATH = "docs/.odds_prev.json"

# ─────────────────────────────────────────────────────────────────────────────
# ATP PLAYER DATABASE
# svpt_won  : P(server wins a point when THIS player is serving)
# rtpt_won  : P(THIS player wins a return point vs any server)
# elo       : surface-specific Elo rating
# birth_year: for age-based fatigue multiplier
# backhand  : "1h" or "2h"
# ─────────────────────────────────────────────────────────────────────────────
ATP_STATS: Dict[str, dict] = {
    "djokovic": {
        "full_name": "Novak Djokovic", "hand": "R", "rank": 2, "country": "SRB",
        "birth_year": 1987, "backhand": "2h",
        "hard":  {"svpt_won": 0.663, "rtpt_won": 0.388, "elo": 2375},
        "clay":  {"svpt_won": 0.652, "rtpt_won": 0.392, "elo": 2420},
        "grass": {"svpt_won": 0.671, "rtpt_won": 0.385, "elo": 2355},
    },
    "alcaraz": {
        "full_name": "Carlos Alcaraz", "hand": "R", "rank": 1, "country": "ESP",
        "birth_year": 2003, "backhand": "2h",
        "hard":  {"svpt_won": 0.658, "rtpt_won": 0.382, "elo": 2300},
        "clay":  {"svpt_won": 0.660, "rtpt_won": 0.390, "elo": 2340},
        "grass": {"svpt_won": 0.670, "rtpt_won": 0.378, "elo": 2285},
    },
    "sinner": {
        "full_name": "Jannik Sinner", "hand": "R", "rank": 1, "country": "ITA",
        "birth_year": 2001, "backhand": "2h",
        "hard":  {"svpt_won": 0.665, "rtpt_won": 0.383, "elo": 2310},
        "clay":  {"svpt_won": 0.655, "rtpt_won": 0.375, "elo": 2265},
        "grass": {"svpt_won": 0.668, "rtpt_won": 0.372, "elo": 2250},
    },
    "medvedev": {
        "full_name": "Daniil Medvedev", "hand": "R", "rank": 5, "country": "RUS",
        "birth_year": 1996, "backhand": "2h",
        "hard":  {"svpt_won": 0.662, "rtpt_won": 0.375, "elo": 2240},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.345, "elo": 2085},
        "grass": {"svpt_won": 0.660, "rtpt_won": 0.355, "elo": 2145},
    },
    "zverev": {
        "full_name": "Alexander Zverev", "hand": "R", "rank": 3, "country": "GER",
        "birth_year": 1997, "backhand": "2h",
        "hard":  {"svpt_won": 0.650, "rtpt_won": 0.360, "elo": 2200},
        "clay":  {"svpt_won": 0.648, "rtpt_won": 0.365, "elo": 2215},
        "grass": {"svpt_won": 0.655, "rtpt_won": 0.352, "elo": 2160},
    },
    "rublev": {
        "full_name": "Andrey Rublev", "hand": "R", "rank": 7, "country": "RUS",
        "birth_year": 1997, "backhand": "2h",
        "hard":  {"svpt_won": 0.635, "rtpt_won": 0.355, "elo": 2120},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.360, "elo": 2140},
        "grass": {"svpt_won": 0.638, "rtpt_won": 0.345, "elo": 2080},
    },
    "tsitsipas": {
        "full_name": "Stefanos Tsitsipas", "hand": "R", "rank": 11, "country": "GRE",
        "birth_year": 1998, "backhand": "1h",
        "hard":  {"svpt_won": 0.638, "rtpt_won": 0.358, "elo": 2110},
        "clay":  {"svpt_won": 0.648, "rtpt_won": 0.370, "elo": 2175},
        "grass": {"svpt_won": 0.645, "rtpt_won": 0.348, "elo": 2065},
    },
    "fritz": {
        "full_name": "Taylor Fritz", "hand": "R", "rank": 4, "country": "USA",
        "birth_year": 1997, "backhand": "2h",
        "hard":  {"svpt_won": 0.660, "rtpt_won": 0.358, "elo": 2155},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.338, "elo": 2020},
        "grass": {"svpt_won": 0.668, "rtpt_won": 0.355, "elo": 2120},
    },
    "de_minaur": {
        "full_name": "Alex de Minaur", "hand": "R", "rank": 9, "country": "AUS",
        "birth_year": 1999, "backhand": "2h",
        "hard":  {"svpt_won": 0.635, "rtpt_won": 0.368, "elo": 2100},
        "clay":  {"svpt_won": 0.628, "rtpt_won": 0.365, "elo": 2070},
        "grass": {"svpt_won": 0.640, "rtpt_won": 0.365, "elo": 2085},
    },
    "hurkacz": {
        "full_name": "Hubert Hurkacz", "hand": "R", "rank": 10, "country": "POL",
        "birth_year": 1997, "backhand": "2h",
        "hard":  {"svpt_won": 0.665, "rtpt_won": 0.348, "elo": 2095},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.325, "elo": 1960},
        "grass": {"svpt_won": 0.678, "rtpt_won": 0.345, "elo": 2110},
    },
    "dimitrov": {
        "full_name": "Grigor Dimitrov", "hand": "R", "rank": 13, "country": "BUL",
        "birth_year": 1991, "backhand": "1h",
        "hard":  {"svpt_won": 0.645, "rtpt_won": 0.355, "elo": 2060},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.348, "elo": 2020},
        "grass": {"svpt_won": 0.652, "rtpt_won": 0.352, "elo": 2045},
    },
    "paul": {
        "full_name": "Tommy Paul", "hand": "R", "rank": 12, "country": "USA",
        "birth_year": 1997, "backhand": "2h",
        "hard":  {"svpt_won": 0.640, "rtpt_won": 0.355, "elo": 2040},
        "clay":  {"svpt_won": 0.632, "rtpt_won": 0.345, "elo": 2005},
        "grass": {"svpt_won": 0.645, "rtpt_won": 0.348, "elo": 2025},
    },
    "auger_aliassime": {
        "full_name": "Felix Auger-Aliassime", "hand": "R", "rank": 20, "country": "CAN",
        "birth_year": 2000, "backhand": "2h",
        "hard":  {"svpt_won": 0.655, "rtpt_won": 0.352, "elo": 2035},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.338, "elo": 1980},
        "grass": {"svpt_won": 0.662, "rtpt_won": 0.348, "elo": 2020},
    },
    "musetti": {
        "full_name": "Lorenzo Musetti", "hand": "L", "rank": 16, "country": "ITA",
        "birth_year": 2002, "backhand": "1h",
        "hard":  {"svpt_won": 0.625, "rtpt_won": 0.348, "elo": 2010},
        "clay":  {"svpt_won": 0.635, "rtpt_won": 0.358, "elo": 2055},
        "grass": {"svpt_won": 0.638, "rtpt_won": 0.348, "elo": 2035},
    },
    "tiafoe": {
        "full_name": "Frances Tiafoe", "hand": "R", "rank": 15, "country": "USA",
        "birth_year": 1998, "backhand": "2h",
        "hard":  {"svpt_won": 0.638, "rtpt_won": 0.352, "elo": 2025},
        "clay":  {"svpt_won": 0.620, "rtpt_won": 0.335, "elo": 1950},
        "grass": {"svpt_won": 0.648, "rtpt_won": 0.345, "elo": 1985},
    },
    "berrettini": {
        "full_name": "Matteo Berrettini", "hand": "R", "rank": 35, "country": "ITA",
        "birth_year": 1996, "backhand": "1h",
        "hard":  {"svpt_won": 0.658, "rtpt_won": 0.345, "elo": 2050},
        "clay":  {"svpt_won": 0.648, "rtpt_won": 0.342, "elo": 2015},
        "grass": {"svpt_won": 0.680, "rtpt_won": 0.345, "elo": 2085},
    },
    "ruud": {
        "full_name": "Casper Ruud", "hand": "R", "rank": 14, "country": "NOR",
        "birth_year": 1998, "backhand": "2h",
        "hard":  {"svpt_won": 0.630, "rtpt_won": 0.348, "elo": 2025},
        "clay":  {"svpt_won": 0.645, "rtpt_won": 0.362, "elo": 2095},
        "grass": {"svpt_won": 0.628, "rtpt_won": 0.332, "elo": 1945},
    },
    "draper": {
        "full_name": "Jack Draper", "hand": "L", "rank": 17, "country": "GBR",
        "birth_year": 2001, "backhand": "2h",
        "hard":  {"svpt_won": 0.648, "rtpt_won": 0.355, "elo": 2020},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.348, "elo": 1985},
        "grass": {"svpt_won": 0.655, "rtpt_won": 0.352, "elo": 2030},
    },
    "shelton": {
        "full_name": "Ben Shelton", "hand": "L", "rank": 21, "country": "USA",
        "birth_year": 2002, "backhand": "2h",
        "hard":  {"svpt_won": 0.658, "rtpt_won": 0.348, "elo": 2000},
        "clay":  {"svpt_won": 0.628, "rtpt_won": 0.325, "elo": 1890},
        "grass": {"svpt_won": 0.668, "rtpt_won": 0.340, "elo": 1985},
    },
    "khachanov": {
        "full_name": "Karen Khachanov", "hand": "R", "rank": 22, "country": "RUS",
        "birth_year": 1996, "backhand": "2h",
        "hard":  {"svpt_won": 0.645, "rtpt_won": 0.345, "elo": 2000},
        "clay":  {"svpt_won": 0.635, "rtpt_won": 0.338, "elo": 1975},
        "grass": {"svpt_won": 0.650, "rtpt_won": 0.335, "elo": 1975},
    },
    "bublik": {
        "full_name": "Alexander Bublik", "hand": "R", "rank": 24, "country": "KAZ",
        "birth_year": 1997, "backhand": "2h",
        "hard":  {"svpt_won": 0.658, "rtpt_won": 0.328, "elo": 1955},
        "clay":  {"svpt_won": 0.635, "rtpt_won": 0.312, "elo": 1880},
        "grass": {"svpt_won": 0.668, "rtpt_won": 0.322, "elo": 1965},
    },
    "humbert": {
        "full_name": "Ugo Humbert", "hand": "L", "rank": 19, "country": "FRA",
        "birth_year": 1998, "backhand": "2h",
        "hard":  {"svpt_won": 0.648, "rtpt_won": 0.355, "elo": 2005},
        "clay":  {"svpt_won": 0.632, "rtpt_won": 0.342, "elo": 1945},
        "grass": {"svpt_won": 0.655, "rtpt_won": 0.348, "elo": 1990},
    },
    "jarry": {
        "full_name": "Nicolas Jarry", "hand": "R", "rank": 28, "country": "CHI",
        "birth_year": 1995, "backhand": "2h",
        "hard":  {"svpt_won": 0.648, "rtpt_won": 0.332, "elo": 1935},
        "clay":  {"svpt_won": 0.645, "rtpt_won": 0.338, "elo": 1955},
        "grass": {"svpt_won": 0.645, "rtpt_won": 0.325, "elo": 1905},
    },
    "cobolli": {
        "full_name": "Flavio Cobolli", "hand": "R", "rank": 30, "country": "ITA",
        "birth_year": 2002, "backhand": "2h",
        "hard":  {"svpt_won": 0.628, "rtpt_won": 0.335, "elo": 1930},
        "clay":  {"svpt_won": 0.635, "rtpt_won": 0.342, "elo": 1965},
        "grass": {"svpt_won": 0.625, "rtpt_won": 0.325, "elo": 1885},
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# WTA PLAYER DATABASE
# ─────────────────────────────────────────────────────────────────────────────
WTA_STATS: Dict[str, dict] = {
    "swiatek": {
        "full_name": "Iga Swiatek", "hand": "R", "rank": 2, "country": "POL",
        "birth_year": 2001, "backhand": "2h",
        "hard":  {"svpt_won": 0.580, "rtpt_won": 0.440, "elo": 2250},
        "clay":  {"svpt_won": 0.590, "rtpt_won": 0.455, "elo": 2355},
        "grass": {"svpt_won": 0.568, "rtpt_won": 0.418, "elo": 2120},
    },
    "sabalenka": {
        "full_name": "Aryna Sabalenka", "hand": "R", "rank": 1, "country": "BLR",
        "birth_year": 1998, "backhand": "2h",
        "hard":  {"svpt_won": 0.598, "rtpt_won": 0.418, "elo": 2215},
        "clay":  {"svpt_won": 0.582, "rtpt_won": 0.408, "elo": 2120},
        "grass": {"svpt_won": 0.595, "rtpt_won": 0.405, "elo": 2145},
    },
    "gauff": {
        "full_name": "Coco Gauff", "hand": "R", "rank": 3, "country": "USA",
        "birth_year": 2004, "backhand": "2h",
        "hard":  {"svpt_won": 0.578, "rtpt_won": 0.415, "elo": 2125},
        "clay":  {"svpt_won": 0.572, "rtpt_won": 0.412, "elo": 2090},
        "grass": {"svpt_won": 0.565, "rtpt_won": 0.400, "elo": 2055},
    },
    "rybakina": {
        "full_name": "Elena Rybakina", "hand": "R", "rank": 7, "country": "KAZ",
        "birth_year": 1999, "backhand": "2h",
        "hard":  {"svpt_won": 0.595, "rtpt_won": 0.408, "elo": 2155},
        "clay":  {"svpt_won": 0.578, "rtpt_won": 0.398, "elo": 2075},
        "grass": {"svpt_won": 0.605, "rtpt_won": 0.408, "elo": 2175},
    },
    "pegula": {
        "full_name": "Jessica Pegula", "hand": "R", "rank": 6, "country": "USA",
        "birth_year": 1994, "backhand": "2h",
        "hard":  {"svpt_won": 0.572, "rtpt_won": 0.405, "elo": 2070},
        "clay":  {"svpt_won": 0.558, "rtpt_won": 0.392, "elo": 1985},
        "grass": {"svpt_won": 0.560, "rtpt_won": 0.388, "elo": 1985},
    },
    "keys": {
        "full_name": "Madison Keys", "hand": "R", "rank": 5, "country": "USA",
        "birth_year": 1995, "backhand": "2h",
        "hard":  {"svpt_won": 0.582, "rtpt_won": 0.395, "elo": 2065},
        "clay":  {"svpt_won": 0.565, "rtpt_won": 0.378, "elo": 1985},
        "grass": {"svpt_won": 0.580, "rtpt_won": 0.380, "elo": 2020},
    },
    "zheng": {
        "full_name": "Qinwen Zheng", "hand": "R", "rank": 8, "country": "CHN",
        "birth_year": 2002, "backhand": "2h",
        "hard":  {"svpt_won": 0.575, "rtpt_won": 0.400, "elo": 2060},
        "clay":  {"svpt_won": 0.568, "rtpt_won": 0.395, "elo": 2035},
        "grass": {"svpt_won": 0.565, "rtpt_won": 0.385, "elo": 2005},
    },
    "paolini": {
        "full_name": "Jasmine Paolini", "hand": "R", "rank": 4, "country": "ITA",
        "birth_year": 1996, "backhand": "2h",
        "hard":  {"svpt_won": 0.562, "rtpt_won": 0.402, "elo": 2050},
        "clay":  {"svpt_won": 0.568, "rtpt_won": 0.410, "elo": 2090},
        "grass": {"svpt_won": 0.555, "rtpt_won": 0.388, "elo": 2010},
    },
    "navarro": {
        "full_name": "Emma Navarro", "hand": "R", "rank": 9, "country": "USA",
        "birth_year": 2001, "backhand": "2h",
        "hard":  {"svpt_won": 0.562, "rtpt_won": 0.395, "elo": 2020},
        "clay":  {"svpt_won": 0.552, "rtpt_won": 0.382, "elo": 1965},
        "grass": {"svpt_won": 0.568, "rtpt_won": 0.392, "elo": 2025},
    },
    "krejcikova": {
        "full_name": "Barbora Krejcikova", "hand": "R", "rank": 10, "country": "CZE",
        "birth_year": 1996, "backhand": "1h",
        "hard":  {"svpt_won": 0.555, "rtpt_won": 0.388, "elo": 1975},
        "clay":  {"svpt_won": 0.565, "rtpt_won": 0.400, "elo": 2025},
        "grass": {"svpt_won": 0.568, "rtpt_won": 0.395, "elo": 2030},
    },
    "sakkari": {
        "full_name": "Maria Sakkari", "hand": "R", "rank": 12, "country": "GRE",
        "birth_year": 1995, "backhand": "2h",
        "hard":  {"svpt_won": 0.562, "rtpt_won": 0.385, "elo": 2000},
        "clay":  {"svpt_won": 0.558, "rtpt_won": 0.382, "elo": 1990},
        "grass": {"svpt_won": 0.555, "rtpt_won": 0.370, "elo": 1955},
    },
    "kasatkina": {
        "full_name": "Daria Kasatkina", "hand": "R", "rank": 15, "country": "RUS",
        "birth_year": 1997, "backhand": "2h",
        "hard":  {"svpt_won": 0.555, "rtpt_won": 0.388, "elo": 1975},
        "clay":  {"svpt_won": 0.562, "rtpt_won": 0.395, "elo": 2005},
        "grass": {"svpt_won": 0.548, "rtpt_won": 0.375, "elo": 1935},
    },
    "kvitova": {
        "full_name": "Petra Kvitova", "hand": "L", "rank": 80, "country": "CZE",
        "birth_year": 1990, "backhand": "2h",
        "hard":  {"svpt_won": 0.575, "rtpt_won": 0.378, "elo": 1955},
        "clay":  {"svpt_won": 0.558, "rtpt_won": 0.360, "elo": 1880},
        "grass": {"svpt_won": 0.590, "rtpt_won": 0.378, "elo": 2010},
    },
    "haddad_maia": {
        "full_name": "Beatriz Haddad Maia", "hand": "L", "rank": 24, "country": "BRA",
        "birth_year": 1996, "backhand": "2h",
        "hard":  {"svpt_won": 0.552, "rtpt_won": 0.378, "elo": 1935},
        "clay":  {"svpt_won": 0.562, "rtpt_won": 0.392, "elo": 1985},
        "grass": {"svpt_won": 0.548, "rtpt_won": 0.368, "elo": 1900},
    },
    "kostyuk": {
        "full_name": "Marta Kostyuk", "hand": "R", "rank": 22, "country": "UKR",
        "birth_year": 2002, "backhand": "2h",
        "hard":  {"svpt_won": 0.562, "rtpt_won": 0.385, "elo": 1975},
        "clay":  {"svpt_won": 0.552, "rtpt_won": 0.378, "elo": 1940},
        "grass": {"svpt_won": 0.558, "rtpt_won": 0.375, "elo": 1945},
    },
    "bencic": {
        "full_name": "Belinda Bencic", "hand": "R", "rank": 45, "country": "SUI",
        "birth_year": 1997, "backhand": "1h",
        "hard":  {"svpt_won": 0.558, "rtpt_won": 0.385, "elo": 1965},
        "clay":  {"svpt_won": 0.548, "rtpt_won": 0.375, "elo": 1920},
        "grass": {"svpt_won": 0.555, "rtpt_won": 0.375, "elo": 1940},
    },
    "collins": {
        "full_name": "Danielle Collins", "hand": "R", "rank": 50, "country": "USA",
        "birth_year": 1994, "backhand": "2h",
        "hard":  {"svpt_won": 0.568, "rtpt_won": 0.385, "elo": 1985},
        "clay":  {"svpt_won": 0.555, "rtpt_won": 0.372, "elo": 1935},
        "grass": {"svpt_won": 0.558, "rtpt_won": 0.365, "elo": 1920},
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# HEAD-TO-HEAD RECORDS
# ─────────────────────────────────────────────────────────────────────────────
H2H: Dict[Tuple[str, str], Tuple[int, int]] = {
    ("djokovic",  "alcaraz"):   (2,  5),
    ("djokovic",  "sinner"):    (5,  2),
    ("alcaraz",   "sinner"):    (5,  4),
    ("djokovic",  "medvedev"):  (12, 5),
    ("djokovic",  "zverev"):    (10, 4),
    ("alcaraz",   "zverev"):    (4,  5),
    ("sinner",    "medvedev"):  (8,  4),
    ("sinner",    "zverev"):    (5,  4),
    ("swiatek",   "sabalenka"): (16, 9),
    ("swiatek",   "gauff"):     (12, 6),
    ("sabalenka", "gauff"):     (7,  5),
    ("swiatek",   "rybakina"):  (8,  6),
    ("sabalenka", "rybakina"):  (7,  4),
    ("gauff",     "rybakina"):  (4,  5),
    ("swiatek",   "keys"):      (9,  3),
    ("sabalenka", "keys"):      (6,  3),
}

# Surface-specific H2H (takes priority when ≥3 matches)
H2H_SURFACE: Dict[Tuple[str, str, str], Tuple[int, int]] = {
    ("djokovic",  "alcaraz",  "clay"):   (1, 3),
    ("djokovic",  "alcaraz",  "hard"):   (1, 2),
    ("djokovic",  "alcaraz",  "grass"):  (0, 1),
    ("djokovic",  "sinner",   "hard"):   (4, 2),
    ("djokovic",  "sinner",   "clay"):   (1, 0),
    ("alcaraz",   "sinner",   "clay"):   (3, 2),
    ("alcaraz",   "sinner",   "hard"):   (2, 2),
    ("alcaraz",   "sinner",   "grass"):  (1, 0),
    ("sinner",    "medvedev", "hard"):   (6, 3),
    ("swiatek",   "sabalenka","clay"):   (10, 3),
    ("swiatek",   "sabalenka","hard"):   (6,  6),
    ("swiatek",   "rybakina", "clay"):   (5,  1),
    ("swiatek",   "rybakina", "hard"):   (3,  5),
}

# BO5 specialist adjustment (Grand Slam format only)
BO5_SPECIALIST: Dict[str, float] = {
    "djokovic": +0.025,
    "sinner":   +0.018,
    "alcaraz":  +0.012,
    "zverev":   +0.008,
    "medvedev": +0.005,
    "ruud":     -0.012,
    "rublev":   -0.010,
    "tsitsipas":-0.008,
    "fritz":    -0.005,
    "de_minaur":-0.006,
}

KELLY_BY_TIER: Dict[str, float] = {"A": 0.30, "B": 0.25, "C": 0.20}

SURFACE_PT_ADJ: Dict[str, float] = {
    "hard":    0.000,
    "clay":   -0.020,
    "grass":  +0.022,
    "carpet": +0.015,
}

TOUR_META: Dict[str, dict] = {
    "grand_slam":  {"name": "大滿貫",    "best_of": 5},
    "masters1000": {"name": "大師賽",    "best_of": 3},
    "wta1000":     {"name": "WTA千人賽", "best_of": 3},
    "atp500":      {"name": "ATP 500",   "best_of": 3},
    "wta500":      {"name": "WTA 500",   "best_of": 3},
    "atp250":      {"name": "ATP 250",   "best_of": 3},
    "wta250":      {"name": "WTA 250",   "best_of": 3},
    "challenger":  {"name": "挑戰賽",    "best_of": 3},
}

# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME CACHES
# ─────────────────────────────────────────────────────────────────────────────
_LIVE_ELO:          Dict[str, dict]  = {}
_LIVE_FORM:         Dict[str, float] = {}
_RECENT_STATS:      Dict[str, dict]  = {}
_INJURIES:          Dict[str, str]   = {}
_SACKMANN_PROFILES: Dict[str, dict]  = {}
_ODDS_PREV:         Dict[str, dict]  = {}  # previous run odds for movement detection

# ─────────────────────────────────────────────────────────────────────────────
# MARKOV CHAIN TENNIS MODEL
# ─────────────────────────────────────────────────────────────────────────────

def game_win_prob(p: float) -> float:
    q = 1.0 - p
    d = p * p + q * q
    if d < 1e-9:
        return 0.5
    p_win_deuce   = p * p / d
    no_deuce      = p ** 4 * (1.0 + 4.0 * q + 10.0 * q ** 2)
    p_reach_deuce = 20.0 * (p ** 3) * (q ** 3)
    return no_deuce + p_reach_deuce * p_win_deuce


def set_win_prob(p1_sv: float, p2_sv: float,
                first_server: int = 1, tiebreak: bool = True) -> float:
    g1 = game_win_prob(p1_sv)
    g2 = game_win_prob(p2_sv)
    tb = (g1 + 1.0 - g2) / 2.0
    memo: Dict[tuple, float] = {}

    def dp(s1: int, s2: int, srv: int) -> float:
        if s1 >= 6 and s1 - s2 >= 2:
            return 1.0
        if s2 >= 6 and s2 - s1 >= 2:
            return 0.0
        if tiebreak and s1 == 6 and s2 == 6:
            return tb
        key = (s1, s2, srv)
        if key in memo:
            return memo[key]
        p_win = g1 if srv == 1 else (1.0 - g2)
        nxt   = 2 if srv == 1 else 1
        val   = p_win * dp(s1 + 1, s2, nxt) + (1.0 - p_win) * dp(s1, s2 + 1, nxt)
        memo[key] = val
        return val

    return dp(0, 0, first_server)


def match_win_prob(p1_sv: float, p2_sv: float, best_of: int = 3) -> float:
    need = (best_of + 1) // 2
    memo: Dict[tuple, float] = {}

    def dp(w1: int, w2: int, srv: int) -> float:
        if w1 == need:
            return 1.0
        if w2 == need:
            return 0.0
        key = (w1, w2, srv)
        if key in memo:
            return memo[key]
        ps  = set_win_prob(p1_sv, p2_sv, first_server=srv)
        nxt = 2 if srv == 1 else 1
        val = ps * dp(w1 + 1, w2, nxt) + (1.0 - ps) * dp(w1, w2 + 1, nxt)
        memo[key] = val
        return val

    return dp(0, 0, 1)


def expected_total_games(p1_sv: float, p2_sv: float,
                         best_of: int = 3, n: int = 4000) -> float:
    g1 = game_win_prob(p1_sv)
    g2 = game_win_prob(p2_sv)
    tb = (g1 + 1.0 - g2) / 2.0
    need  = (best_of + 1) // 2
    total = 0

    for _ in range(n):
        games = 0
        w1 = w2 = 0
        srv = 1
        while w1 < need and w2 < need:
            s1 = s2 = 0
            while True:
                p_win = g1 if srv == 1 else (1.0 - g2)
                if random.random() < p_win:
                    s1 += 1
                else:
                    s2 += 1
                games += 1
                srv = 2 if srv == 1 else 1
                if (s1 >= 6 and s1 - s2 >= 2) or (s2 >= 6 and s2 - s1 >= 2):
                    break
                if s1 == 6 and s2 == 6:
                    s1 = 7 if random.random() < tb else s1
                    s2 = 7 if s1 != 7 else s2
                    if s1 == 7 or s2 == 7:
                        games += 1
                    break
            if s1 > s2:
                w1 += 1
            else:
                w2 += 1
        total += games

    return total / n


# ─────────────────────────────────────────────────────────────────────────────
# ELO MODEL
# ─────────────────────────────────────────────────────────────────────────────
ELO_SCALE = 400.0


def elo_win_prob(elo1: float, elo2: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((elo2 - elo1) / ELO_SCALE))


def h2h_adj(p1: str, p2: str, surface: str = "") -> float:
    # Try surface-specific H2H first (min 3 matches)
    if surface:
        if (p1, p2, surface) in H2H_SURFACE:
            sw1, sw2 = H2H_SURFACE[(p1, p2, surface)]
        elif (p2, p1, surface) in H2H_SURFACE:
            sw2, sw1 = H2H_SURFACE[(p2, p1, surface)]
        else:
            sw1, sw2 = 0, 0
        if sw1 + sw2 >= 3:
            return max(-0.05, min(0.05, (sw1 / (sw1 + sw2) - 0.5) * 0.10))
    # Fall back to overall H2H
    w1, w2 = 0, 0
    if (p1, p2) in H2H:
        w1, w2 = H2H[(p1, p2)]
    elif (p2, p1) in H2H:
        w2, w1 = H2H[(p2, p1)]
    total = w1 + w2
    if total < 4:
        return 0.0
    return max(-0.05, min(0.05, (w1 / total - 0.5) * 0.10))


# ─────────────────────────────────────────────────────────────────────────────
# FATIGUE & HOLD/BREAK MODELS
# ─────────────────────────────────────────────────────────────────────────────

def fatigue_score(days_rest: int, prev_minutes: float, sets_played: int) -> float:
    """Return 0–10 fatigue score. Higher = more fatigued."""
    score = 0.0
    if days_rest == 0:
        score += 4.0
    elif days_rest == 1:
        score += 2.5
    elif days_rest == 2:
        score += 1.0
    elif days_rest >= 6:
        score -= 1.0
    if prev_minutes > 180:
        score += 3.0
    elif prev_minutes > 120:
        score += 1.5
    if sets_played >= 4:
        score += 2.0
    elif sets_played == 3:
        score += 0.8
    return max(0.0, min(10.0, score))


def hold_break_win_prob(hold1: float, break1: float,
                        hold2: float, break2: float) -> float:
    """P(p1 wins) from dominance ratio of hold+break rates."""
    dom1 = (hold1 + break1) / 2.0
    dom2 = (hold2 + break2) / 2.0
    return dom1 / (dom1 + dom2) if (dom1 + dom2) > 1e-9 else 0.5


# ─────────────────────────────────────────────────────────────────────────────
# ADVANCED ADJUSTMENT FUNCTIONS  (v3)
# ─────────────────────────────────────────────────────────────────────────────

def get_court_speed_adj(sport_key: str, tournament: str = "") -> float:
    """Additive boost/penalty to svpt_won for court speed / indoor."""
    t = (sport_key + " " + tournament).lower()
    base = 0.0
    if any(x in t for x in INDOOR_TOURNAMENTS):
        base += 0.012
    for name, adj in COURT_SPEED_ADJ.items():
        if name in t:
            base += adj
            break
    return max(-0.025, min(0.025, base))


def age_fatigue_mult(player_key: str) -> float:
    """Players over 28 accumulate fatigue faster; returns multiplier >= 1.0."""
    all_players = {**ATP_STATS, **WTA_STATS}
    by = all_players.get(player_key, {}).get("birth_year")
    if not by:
        return 1.0
    age = datetime.datetime.utcnow().year - by
    if age <= 28:
        return 1.0
    return min(1.6, 1.0 + (age - 28) * AGE_FATIGUE_SCALE)


def lefty_matchup_adj(p1_key: str, p2_key: str, surface: str) -> float:
    """Serve point bonus when left-hander serves against right-hander."""
    all_players = {**ATP_STATS, **WTA_STATS}
    h1 = all_players.get(p1_key, {}).get("hand", "R")
    h2 = all_players.get(p2_key, {}).get("hand", "R")
    if h1 == h2:
        return 0.0
    bonus = LEFTY_SERVE_BONUS
    if surface == "grass":
        bonus += LEFTY_GRASS_EXTRA
    return bonus if h1 == "L" else -bonus


def backhand_matchup_adj(p1_key: str, p2_key: str, surface: str) -> float:
    """1h backhand vulnerability vs heavy lefty topspin on clay."""
    if surface != "clay":
        return 0.0
    all_players = {**ATP_STATS, **WTA_STATS}
    bh1 = all_players.get(p1_key, {}).get("backhand", "2h")
    bh2 = all_players.get(p2_key, {}).get("backhand", "2h")
    h1  = all_players.get(p1_key, {}).get("hand", "R")
    h2  = all_players.get(p2_key, {}).get("hand", "R")
    adj = 0.0
    if bh1 == "1h" and h2 == "L":
        adj -= BH_TOPSPIN_VULN
    if bh2 == "1h" and h1 == "L":
        adj += BH_TOPSPIN_VULN
    return adj


def clutch_adj(p1_key: str, p2_key: str) -> float:
    """Tiebreak + break point save + deciding set record."""
    prof1 = _SACKMANN_PROFILES.get(p1_key, {})
    prof2 = _SACKMANN_PROFILES.get(p2_key, {})
    adj   = 0.0
    tb1 = prof1.get("tb_win_pct");  tb2 = prof2.get("tb_win_pct")
    if tb1 is not None and tb2 is not None:
        adj += (tb1 - tb2) * 0.10
    bp1 = prof1.get("bp_save_pct"); bp2 = prof2.get("bp_save_pct")
    if bp1 is not None and bp2 is not None:
        adj += (bp1 - bp2) * 0.06
    dc1 = prof1.get("deciding_pct"); dc2 = prof2.get("deciding_pct")
    if dc1 is not None and dc2 is not None:
        adj += (dc1 - dc2) * 0.06
    return max(-0.07, min(0.07, adj))


def df_penalty_adj(p1_key: str, p2_key: str, is_wta: bool) -> float:
    """Double fault rate differential; more impact in WTA."""
    prof1 = _SACKMANN_PROFILES.get(p1_key, {})
    prof2 = _SACKMANN_PROFILES.get(p2_key, {})
    df1 = prof1.get("df_rate"); df2 = prof2.get("df_rate")
    if df1 is None or df2 is None:
        return 0.0
    scale = 0.35 if is_wta else 0.22
    return max(-0.04, min(0.04, (df2 - df1) * scale))


def surface_form_adj(p1_key: str, p2_key: str, surface: str) -> float:
    """Surface-specific recent win rate differential."""
    prof1 = _SACKMANN_PROFILES.get(p1_key, {})
    prof2 = _SACKMANN_PROFILES.get(p2_key, {})
    sf1 = prof1.get("surface_form", {}).get(surface)
    sf2 = prof2.get("surface_form", {}).get(surface)
    if sf1 is None or sf2 is None:
        return 0.0
    return max(-0.04, min(0.04, (sf1 - sf2) * 0.12))


def ace_serve_adj(p1_key: str, p2_key: str) -> float:
    """Ace rate differential as additional serve dominance signal."""
    prof1 = _SACKMANN_PROFILES.get(p1_key, {})
    prof2 = _SACKMANN_PROFILES.get(p2_key, {})
    a1 = prof1.get("ace_rate"); a2 = prof2.get("ace_rate")
    if a1 is None or a2 is None:
        return 0.0
    return max(-0.025, min(0.025, (a1 - a2) * 0.40))


def altitude_adj(tournament: str, surface: str) -> float:
    """Higher altitude → ball travels faster; especially offsets clay slowness."""
    t = tournament.lower().replace(" ", "_").replace("-", "_")
    for city, alt_m in ALTITUDE_M.items():
        if city in t:
            if alt_m < 400:
                return 0.0
            base = min(0.020, (alt_m - 200) / 500 * 0.005)
            return round(base * (1.5 if surface == "clay" else 1.0), 4)
    return 0.0


def fetch_wind(lat: float, lon: float) -> Optional[float]:
    """Wind speed km/h via free Open-Meteo API."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    "current": "wind_speed_10m,precipitation",
                    "wind_speed_unit": "kmh", "forecast_days": 1},
            timeout=6,
        )
        r.raise_for_status()
        cur = r.json().get("current", {})
        return cur.get("wind_speed_10m"), cur.get("precipitation", 0.0)
    except Exception as e:
        log.debug("fetch_wind (%s,%s): %s", lat, lon, e)
        return None, 0.0


def wind_adj(tournament: str, surface: str, p1_key: str, p2_key: str) -> Tuple[float, float]:
    """
    Wind penalises heavy topspin/clay-court baseline play.
    Returns (prob_adj_for_p1, wind_kmh).
    Only applied for outdoor clay/grass.
    """
    if surface not in ("clay", "grass"):
        return 0.0, 0.0
    coords = None
    t = tournament.lower().replace(" ", "_").replace("-", "_")
    for name, (lat, lon) in TOURNAMENT_COORDS.items():
        if name in t or t in name:
            coords = (lat, lon)
            break
    if not coords:
        return 0.0, 0.0
    wind, rain = fetch_wind(*coords)
    if wind is None or wind < 15:
        return 0.0, wind or 0.0
    # Aggressive servers benefit more in wind; topspin players suffer
    a1 = _SACKMANN_PROFILES.get(p1_key, {}).get("ace_rate") or 0.06
    a2 = _SACKMANN_PROFILES.get(p2_key, {}).get("ace_rate") or 0.06
    factor = min(0.030, (wind - 15) * 0.0012)
    adj = (a1 - a2) * factor * 4.0
    return max(-0.025, min(0.025, adj)), round(wind, 1)


def win_streak_adj(p1_key: str, p2_key: str) -> float:
    """Recent win/loss streak as momentum signal. ±3% max."""
    prof1 = _SACKMANN_PROFILES.get(p1_key, {})
    prof2 = _SACKMANN_PROFILES.get(p2_key, {})
    s1 = prof1.get("win_streak", 0)
    s2 = prof2.get("win_streak", 0)

    def bonus(s: int) -> float:
        if s >= 5:  return 0.030
        if s >= 4:  return 0.022
        if s >= 3:  return 0.015
        if s <= -4: return -0.025
        if s <= -3: return -0.015
        return 0.0

    return max(-0.04, min(0.04, bonus(s1) - bonus(s2)))


def bo5_adj(p1_key: str, p2_key: str, best_of: int) -> float:
    """Grand Slam specialist advantage (best-of-5 only)."""
    if best_of != 5:
        return 0.0
    sp1 = BO5_SPECIALIST.get(p1_key, 0.0)
    sp2 = BO5_SPECIALIST.get(p2_key, 0.0)
    return max(-0.05, min(0.05, sp1 - sp2))


def first_serve_adj(p1_key: str, p2_key: str) -> float:
    """1st serve % differential → consistent server advantage (±1.5%)."""
    p1 = _SACKMANN_PROFILES.get(p1_key, {})
    p2 = _SACKMANN_PROFILES.get(p2_key, {})
    f1 = p1.get("first_serve_pct")
    f2 = p2.get("first_serve_pct")
    if f1 is None or f2 is None:
        return 0.0
    return max(-0.015, min(0.015, (f1 - f2) * 0.15))


def bp_attack_adj(p1_key: str, p2_key: str) -> float:
    """Break point conversion rate differential (±3%)."""
    p1 = _SACKMANN_PROFILES.get(p1_key, {})
    p2 = _SACKMANN_PROFILES.get(p2_key, {})
    c1 = p1.get("bp_conv_pct")
    c2 = p2.get("bp_conv_pct")
    if c1 is None or c2 is None:
        return 0.0
    return max(-0.03, min(0.03, (c1 - c2) * 0.12))


def conditioning_adj(p1_key: str, p2_key: str) -> float:
    """Heavy recent match load penalty (>6 matches in 14 days → fatigue flag)."""
    p1 = _SACKMANN_PROFILES.get(p1_key, {})
    p2 = _SACKMANN_PROFILES.get(p2_key, {})
    m1 = p1.get("matches_last_14d", 3)
    m2 = p2.get("matches_last_14d", 3)

    def penalty(m: int) -> float:
        if m >= 8:  return -0.030
        if m >= 7:  return -0.020
        if m >= 6:  return -0.010
        return 0.0

    return max(-0.04, min(0.04, penalty(m2) - penalty(m1)))


def compute_elo_from_sackmann(all_matches: List[dict]) -> None:
    """
    Derive surface-specific ELO from Sackmann match history and store in _LIVE_ELO.
    Uses K=48 for Grand Slams, K=40 for Masters/Finals, K=32 otherwise.
    """
    all_db = {**ATP_STATS, **WTA_STATS}
    elos: Dict[str, Dict[str, float]] = {}

    for row in sorted(all_matches, key=lambda r: r.get("tourney_date", "19000101")):
        wname = (row.get("winner_name") or "").lower()
        lname = (row.get("loser_name") or "").lower()
        wkey  = norm_player(wname)
        lkey  = norm_player(lname)
        if not wkey or not lkey:
            continue

        surf_raw = (row.get("surface") or "hard").lower()
        surf = surf_raw if surf_raw in ("hard", "clay", "grass") else "hard"

        for key in (wkey, lkey):
            if key not in elos:
                base = float(all_db.get(key, {}).get(surf, {}).get("elo", 1500))
                elos[key] = {"hard": base, "clay": base, "grass": base}

        ew = elos[wkey][surf]
        el = elos[lkey][surf]
        exp_w = 1.0 / (1.0 + 10.0 ** ((el - ew) / 400.0))

        lvl = (row.get("tourney_level") or "").upper()
        k   = 48 if lvl == "G" else 40 if lvl in ("M", "F") else 32

        elos[wkey][surf] = ew + k * (1.0 - exp_w)
        elos[lkey][surf] = el - k * (1.0 - exp_w)

    # Store ELO for ALL computed players (not just those in static dict)
    for key, surf_elos in elos.items():
        _LIVE_ELO[key] = {s: round(v, 1) for s, v in surf_elos.items()}
    log.info("compute_elo_from_sackmann: %d players", len(elos))


def load_odds_prev() -> Dict[str, dict]:
    try:
        with open(ODDS_PREV_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_odds_prev(odds_map: Dict[str, dict]) -> None:
    os.makedirs("docs", exist_ok=True)
    try:
        snapshot = {k: {"best_home": v["best_home"], "best_away": v["best_away"]}
                    for k, v in odds_map.items()}
        with open(ODDS_PREV_PATH, "w") as f:
            json.dump(snapshot, f)
    except Exception as e:
        log.warning("save_odds_prev: %s", e)


def odds_move_signal(key: str, odds_info: dict,
                     prev: Dict[str, dict]) -> Tuple[float, str]:
    """
    Detect significant odds movement (sharp money indicator).
    Returns (prob_adj_for_home, label).
    """
    if key not in prev:
        return 0.0, ""
    ph = prev[key].get("best_home", odds_info["best_home"])
    pa = prev[key].get("best_away", odds_info["best_away"])
    ch = odds_info["best_home"]
    ca = odds_info["best_away"]

    def to_p(o: float) -> float:
        return 1.0 / o if o > 1.0 else 0.0

    shift = to_p(ch) - to_p(ph)  # positive = home shorted (steam on home)
    if abs(shift) < 0.025:
        return 0.0, ""

    label = "steam_home" if shift > 0 else "steam_away"
    adj   = max(-0.04, min(0.04, shift * 0.40))
    log.info("odds_move %s: shift=%+.3f -> %s adj=%+.4f", key, shift, label, adj)
    return adj, label


def detect_injuries(all_matches: List[dict]) -> set:
    """
    Scan last 14 days for RET/W/O results to flag potentially injured players.
    """
    cutoff  = datetime.datetime.utcnow() - datetime.timedelta(days=14)
    injured: set = set()
    for row in all_matches:
        try:
            md = datetime.datetime.strptime(
                str(row.get("tourney_date", "19000101")), "%Y%m%d")
        except ValueError:
            continue
        if md < cutoff:
            continue
        score = (row.get("score") or "").upper()
        if "RET" not in score and "W/O" not in score:
            continue
        loser = (row.get("loser_name") or "").lower()
        if loser:
            key = norm_player(loser)
            all_db = {**ATP_STATS, **WTA_STATS}
            if key in all_db:
                injured.add(key)
                log.info("auto-injury: %s (%s)", key, row.get("loser_name"))
    return injured


def extract_tournament(sport_key: str, game: dict) -> str:
    """Derive a normalised tournament name from sport_key / sport_title."""
    sk = sport_key.lower()
    for token in ("french_open", "wimbledon", "us_open", "australian_open"):
        if token in sk:
            return token
    title = game.get("sport_title", "").lower()
    return title.replace(" ", "_").replace("-", "_")


# ─────────────────────────────────────────────────────────────────────────────
# PLAYER LOOKUP
# ─────────────────────────────────────────────────────────────────────────────
_ALIASES: Dict[str, str] = {
    "novak djokovic":        "djokovic",
    "carlos alcaraz":        "alcaraz",
    "jannik sinner":         "sinner",
    "daniil medvedev":       "medvedev",
    "alexander zverev":      "zverev",
    "andrey rublev":         "rublev",
    "stefanos tsitsipas":    "tsitsipas",
    "taylor fritz":          "fritz",
    "alex de minaur":        "de_minaur",
    "hubert hurkacz":        "hurkacz",
    "grigor dimitrov":       "dimitrov",
    "tommy paul":            "paul",
    "felix auger-aliassime": "auger_aliassime",
    "felix auger aliassime": "auger_aliassime",
    "lorenzo musetti":       "musetti",
    "frances tiafoe":        "tiafoe",
    "matteo berrettini":     "berrettini",
    "casper ruud":           "ruud",
    "jack draper":           "draper",
    "karen khachanov":       "khachanov",
    "ben shelton":           "shelton",
    "alexander bublik":      "bublik",
    "ugo humbert":           "humbert",
    "nicolas jarry":         "jarry",
    "flavio cobolli":        "cobolli",
    "iga swiatek":           "swiatek",
    "aryna sabalenka":       "sabalenka",
    "coco gauff":            "gauff",
    "elena rybakina":        "rybakina",
    "jessica pegula":        "pegula",
    "madison keys":          "keys",
    "qinwen zheng":          "zheng",
    "jasmine paolini":       "paolini",
    "emma navarro":          "navarro",
    "barbora krejcikova":    "krejcikova",
    "maria sakkari":         "sakkari",
    "daria kasatkina":       "kasatkina",
    "petra kvitova":         "kvitova",
    "beatriz haddad maia":   "haddad_maia",
    "marta kostyuk":         "kostyuk",
    "belinda bencic":        "bencic",
    "danielle collins":      "collins",
}


CHINESE_NAMES: Dict[str, str] = {
    # ATP Top players
    "Novak Djokovic": "德約科維奇", "Carlos Alcaraz": "阿爾卡拉斯",
    "Jannik Sinner": "辛納", "Daniil Medvedev": "梅德韋杰夫",
    "Alexander Zverev": "茲韋列夫", "Andrey Rublev": "魯布列夫",
    "Stefanos Tsitsipas": "西西帕斯", "Taylor Fritz": "弗里茨",
    "Alex de Minaur": "德米諾爾", "Hubert Hurkacz": "胡卡茲",
    "Grigor Dimitrov": "季米特洛夫", "Tommy Paul": "保羅",
    "Felix Auger-Aliassime": "奧熱-阿利亞西姆", "Lorenzo Musetti": "穆塞蒂",
    "Frances Tiafoe": "蒂亞福", "Matteo Berrettini": "貝雷蒂尼",
    "Casper Ruud": "魯德", "Jack Draper": "德雷珀",
    "Karen Khachanov": "哈恰諾夫", "Ben Shelton": "謝爾頓",
    "Alexander Bublik": "布布利克", "Ugo Humbert": "翁貝爾",
    "Nicolas Jarry": "哈里", "Flavio Cobolli": "科博利",
    "Holger Rune": "魯內", "Jiri Lehecka": "萊赫卡",
    "Sebastian Korda": "科爾達", "Alexei Popyrin": "波普林",
    "Lorenzo Sonego": "索內戈", "Arthur Fils": "費爾斯",
    "Tallon Griekspoor": "格里克斯普爾", "Matteo Arnaldi": "阿納爾迪",
    "Hugo Gaston": "加斯頓", "Gael Monfils": "孟菲爾斯",
    "Ethan Quinn": "奎恩", "Francisco Comesana": "科梅薩尼亞",
    "Sebastian Baez": "巴耶斯", "Tomas Etcheverry": "埃切維里",
    "Luciano Darderi": "達德里", "Alejandro Tabilo": "塔比羅",
    "Alejandro Davidovich Fokina": "達維多維奇", "Francisco Cerundolo": "切倫杜羅",
    "Adrian Mannarino": "曼納里諾", "Corentin Moutet": "穆泰",
    "Giovanni Mpetshi Perricard": "佩里卡爾", "Roberto Bautista Agut": "鮑蒂斯塔",
    "David Goffin": "高芬", "Borna Coric": "科里奇",
    "Stan Wawrinka": "瓦林卡", "Rafael Nadal": "納達爾",
    # WTA Top players
    "Iga Swiatek": "斯維亞泰克", "Aryna Sabalenka": "莎巴蘭卡",
    "Coco Gauff": "高芙", "Elena Rybakina": "里巴金娜",
    "Jessica Pegula": "佩古拉", "Madison Keys": "基斯",
    "Qinwen Zheng": "鄭欽文", "Jasmine Paolini": "保利尼",
    "Emma Navarro": "納瓦羅", "Barbora Krejcikova": "克雷奇科娃",
    "Maria Sakkari": "薩卡里", "Daria Kasatkina": "卡薩特金娜",
    "Petra Kvitova": "科維托娃", "Beatriz Haddad Maia": "阿達德·瑪雅",
    "Marta Kostyuk": "科斯秋克", "Belinda Bencic": "本西奇",
    "Danielle Collins": "柯林斯", "Dayana Yastremska": "雅斯特雷姆斯卡",
    "Ons Jabeur": "賈比爾", "Caroline Garcia": "加西亞",
    "Paula Badosa": "巴多薩", "Elina Svitolina": "斯維托利娜",
    "Mirra Andreeva": "安德烈耶娃", "Diana Shnaider": "施奈德",
    "Liudmila Samsonova": "薩姆索諾娃", "Victoria Azarenka": "阿紮倫卡",
    "Simona Halep": "哈勒普", "Anhelina Kalinina": "卡利尼娜",
    "Elena-Gabriela Ruse": "魯塞", "Clara Burel": "比里爾",
    "Ekaterina Alexandrova": "亞歷山德羅娃", "Anna Kalinskaya": "卡林斯卡婭",
    "Veronika Kudermetova": "庫德梅托娃", "Anastasia Pavlyuchenkova": "帕夫柳琴科娃",
}


def cn_name(full_name: str) -> str:
    """Return Chinese name if known, else the last word of the English name."""
    if not full_name:
        return full_name
    if full_name in CHINESE_NAMES:
        return CHINESE_NAMES[full_name]
    nl = full_name.lower()
    for en, cn in CHINESE_NAMES.items():
        if en.lower() == nl:
            return cn
    return full_name.split()[-1] if full_name.split() else full_name


def norm_player(name: str) -> str:
    n = name.lower().strip()
    if n in _ALIASES:
        return _ALIASES[n]
    last = n.split()[-1] if n.split() else n
    for alias, key in _ALIASES.items():
        if alias.split()[-1] == last:
            return key
    return n.replace(" ", "_").replace("-", "_")


def get_surface_stats(key: str, surface: str) -> dict:
    players = {**ATP_STATS, **WTA_STATS}
    surf = surface if surface in ("hard", "clay", "grass") else "hard"
    base = dict(players.get(key, {}).get(surf,
           {"svpt_won": 0.610, "rtpt_won": 0.330, "elo": 1500}))
    live_elo = _LIVE_ELO.get(key, {}).get(surf)
    if live_elo:
        base["elo"] = live_elo
    rec = _RECENT_STATS.get(key, {})
    if rec.get("svpt_won"):
        base["svpt_won"] = base["svpt_won"] * 0.5 + rec["svpt_won"] * 0.5
        base["rtpt_won"] = base["rtpt_won"] * 0.5 + rec.get("rtpt_won", base["rtpt_won"]) * 0.5
    return base


def infer_surface(sport_key: str, tournament: str = "") -> str:
    t = (sport_key + " " + tournament).lower()
    if any(x in t for x in ["clay", "french", "roland", "madrid", "rome",
                              "barcelona", "monte_carlo", "monte-carlo"]):
        return "clay"
    if any(x in t for x in ["grass", "wimbledon", "queens", "halle",
                              "eastbourne", "s-hertogenbosch"]):
        return "grass"
    return "hard"


def infer_tour_level(sport_key: str, tournament: str = "") -> str:
    t = (sport_key + " " + tournament).lower()
    is_wta = "wta" in t
    if any(x in t for x in ["australian", "french", "wimbledon", "us_open",
                              "us open", "roland", "grand_slam"]):
        return "grand_slam"
    if any(x in t for x in ["masters", "1000", "indian_wells", "miami",
                              "montreal", "toronto", "cincinnati", "shanghai",
                              "paris", "rome"]):
        return "wta1000" if is_wta else "masters1000"
    if any(x in t for x in ["500", "dubai", "acapulco", "barcelona",
                              "washington", "hamburg"]):
        return "wta500" if is_wta else "atp500"
    return "wta250" if is_wta else "atp250"


# ─────────────────────────────────────────────────────────────────────────────
# JEFF SACKMANN DATA — ROLLING FORM + FATIGUE + ADVANCED STATS
# ─────────────────────────────────────────────────────────────────────────────

def _calc_svpt_won(row: dict, prefix: str = "w") -> Optional[float]:
    """Derive serve point win % from a Sackmann match CSV row."""
    try:
        svpt = float(row.get(f"{prefix}_svpt") or 0)
        in1  = float(row.get(f"{prefix}_1stIn") or 0)
        won1 = float(row.get(f"{prefix}_1stWon") or 0)
        won2 = float(row.get(f"{prefix}_2ndWon") or 0)
        if svpt < 20:
            return None
        fsp = in1 / svpt
        fsw = won1 / in1 if in1 > 0 else 0.68
        ssw = won2 / max(1.0, svpt - in1)
        return fsp * fsw + (1.0 - fsp) * ssw
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def _name_matches(csv_name: str, full_name: str) -> bool:
    """Check if a Sackmann CSV 'First Last' name matches our full_name."""
    cl    = csv_name.lower().strip()
    parts = full_name.lower().split()
    if not parts or not cl:
        return False
    last = parts[-1]
    if last not in cl:
        return False
    if len(last) < 6:
        first     = parts[0][0] if parts[0] else ""
        csv_parts = cl.split()
        csv_first = csv_parts[0][0] if csv_parts and csv_parts[0] else ""
        return first == csv_first
    return True


def fetch_sackmann_matches(year: int = None) -> List[dict]:
    """Download ATP + WTA match CSVs. Falls back to prev year if < 300 rows."""
    if year is None:
        year = datetime.datetime.utcnow().year
    rows: List[dict] = []
    for y in [year, year - 1]:
        year_rows = 0
        for tour in ("atp", "wta"):
            url = (f"https://raw.githubusercontent.com/JeffSackmann/tennis_{tour}"
                   f"/master/{tour}_matches_{y}.csv")
            try:
                r = requests.get(url, timeout=25)
                r.raise_for_status()
                batch = list(csv.DictReader(io.StringIO(r.text)))
                rows.extend(batch)
                year_rows += len(batch)
                log.info("fetch_sackmann: %s_%d → %d rows", tour, y, len(batch))
            except Exception as e:
                log.warning("fetch_sackmann %s_%d: %s", tour, y, e)
        if y == year and year_rows >= 300:
            break
    return rows


def build_player_profile(all_matches: List[dict], full_name: str,
                         n: int = 20) -> Optional[dict]:
    """Compute rolling serve/return/form/fatigue + advanced stats from last n matches."""
    player_rows: List[Tuple[dict, bool]] = []
    for row in all_matches:
        wname = row.get("winner_name", "")
        lname = row.get("loser_name", "")
        if _name_matches(wname, full_name):
            player_rows.append((row, True))
        elif _name_matches(lname, full_name):
            player_rows.append((row, False))

    if not player_rows:
        return None

    player_rows.sort(key=lambda x: x[0].get("tourney_date", "0"), reverse=True)
    recent = player_rows[:n]

    sv_wons, rt_wons, results, mins_list, sets_list = [], [], [], [], []
    df_list:   List[float] = []
    ace_list:  List[float] = []
    fs_pct_list:  List[float] = []  # 1st serve %
    ss_win_list:  List[float] = []  # 2nd serve win %
    bp_conv_num,  bp_conv_den = 0, 0  # BP conversion (attack)
    tb_won, tb_total        = 0, 0
    bp_saved, bp_faced      = 0, 0
    dec_won, dec_total      = 0, 0
    now_utc = datetime.datetime.utcnow()
    cutoff14 = now_utc - datetime.timedelta(days=14)
    matches_14d = 0
    surface_res: Dict[str, List[int]] = {"hard": [], "clay": [], "grass": []}

    def _sf(val) -> Optional[float]:
        try:
            v = float(val or 0)
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None

    for row, is_winner in recent:
        prefix     = "w" if is_winner else "l"
        opp_prefix = "l" if is_winner else "w"

        sv     = _calc_svpt_won(row, prefix)
        rt_opp = _calc_svpt_won(row, opp_prefix)
        if sv is not None:
            sv_wons.append(sv)
        if rt_opp is not None:
            rt_wons.append(1.0 - rt_opp)

        results.append(1 if is_winner else 0)

        try:
            m = float(row.get("minutes") or 0)
            if m > 0:
                mins_list.append(m)
        except (ValueError, TypeError):
            pass

        score = row.get("score", "") or ""
        sets  = [s for s in score.split() if "-" in s and not s.startswith("RET")]
        sets_list.append(max(1, len(sets)))

        svpt  = _sf(row.get(f"{prefix}_svpt"))
        in1st = _sf(row.get(f"{prefix}_1stIn"))
        w2nd  = _sf(row.get(f"{prefix}_2ndWon"))
        df    = _sf(row.get(f"{prefix}_df"))
        ace   = _sf(row.get(f"{prefix}_ace"))

        if svpt and svpt >= 20:
            if df is not None:
                df_list.append(df / svpt)
            if ace is not None:
                ace_list.append(ace / svpt)
            if in1st is not None:
                fs_pct_list.append(in1st / svpt)
                sec_svpt = svpt - in1st
                if w2nd is not None and sec_svpt > 0:
                    ss_win_list.append(w2nd / sec_svpt)

        # BP conversion (attacking): opp prefix tells us our chances
        opp = "l" if is_winner else "w"
        try:
            opp_bpf = int(row.get(f"{opp}_bpFaced") or 0)
            opp_bps = int(row.get(f"{opp}_bpSaved") or 0)
            if opp_bpf > 0:
                bp_conv_num += opp_bpf - opp_bps  # BPs we converted
                bp_conv_den += opp_bpf
        except (ValueError, TypeError):
            pass

        # Match load in last 14 days
        td_str = row.get("tourney_date", "")
        if len(td_str) == 8:
            try:
                md = datetime.datetime(int(td_str[:4]), int(td_str[4:6]), int(td_str[6:8]))
                if md >= cutoff14:
                    matches_14d += 1
            except ValueError:
                pass

        for s in sets:
            base = s.split("(")[0]
            if base == "7-6":
                tb_total += 1
                if is_winner:
                    tb_won += 1
            elif base == "6-7":
                tb_total += 1
                if not is_winner:
                    tb_won += 1

        try:
            bpf = int(row.get(f"{prefix}_bpFaced") or 0)
            bps = int(row.get(f"{prefix}_bpSaved") or 0)
            if bpf > 0:
                bp_faced += bpf
                bp_saved += bps
        except (ValueError, TypeError):
            pass

        if len(sets) >= 3:
            dec_total += 1
            if is_winner:
                dec_won += 1

        surf_raw = (row.get("surface") or "hard").lower()
        surf_key = surf_raw if surf_raw in surface_res else "hard"
        surface_res[surf_key].append(1 if is_winner else 0)

    weights   = [1.0 / (i + 1.0) for i in range(len(results))]
    total_w   = sum(weights)
    form_rate = sum(r * w for r, w in zip(results, weights)) / total_w if total_w > 0 else 0.5

    last_date_str = recent[0][0].get("tourney_date", "") or ""
    days_rest = 3
    if len(last_date_str) == 8:
        try:
            ld = datetime.datetime(
                int(last_date_str[:4]),
                int(last_date_str[4:6]),
                int(last_date_str[6:8]),
            )
            days_rest = max(0, (datetime.datetime.utcnow() - ld).days)
        except ValueError:
            pass

    surf_form = {
        s: round(sum(v) / len(v), 4)
        for s, v in surface_res.items() if len(v) >= 3
    }

    # Win streak: positive = consecutive wins, negative = consecutive losses
    win_streak = 0
    if results:
        direction = results[0]  # 1=win, 0=loss
        for r in results:
            if r == direction:
                win_streak += 1 if direction else -1
            else:
                break

    return {
        "svpt_won":     round(sum(sv_wons) / len(sv_wons), 4) if sv_wons else None,
        "rtpt_won":     round(sum(rt_wons) / len(rt_wons), 4) if rt_wons else None,
        "form_rate":    round(form_rate, 4),
        "n_matches":    len(recent),
        "days_rest":    days_rest,
        "last_minutes": mins_list[0] if mins_list else 90.0,
        "avg_minutes":  round(sum(mins_list) / len(mins_list), 1) if mins_list else 90.0,
        "last_sets":    sets_list[0] if sets_list else 3,
        "df_rate":      round(sum(df_list)  / len(df_list),  5) if df_list  else None,
        "ace_rate":     round(sum(ace_list) / len(ace_list), 5) if ace_list else None,
        "tb_win_pct":   round(tb_won / tb_total, 4) if tb_total >= 3 else None,
        "bp_save_pct":  round(bp_saved / bp_faced, 4) if bp_faced >= 5 else None,
        "deciding_pct": round(dec_won / dec_total, 4) if dec_total >= 3 else None,
        "surface_form":       surf_form,
        "win_streak":         win_streak,
        "first_serve_pct":    round(sum(fs_pct_list) / len(fs_pct_list), 4) if fs_pct_list  else None,
        "second_serve_win":   round(sum(ss_win_list) / len(ss_win_list), 4) if ss_win_list  else None,
        "bp_conv_pct":        round(bp_conv_num / bp_conv_den, 4) if bp_conv_den >= 5 else None,
        "matches_last_14d":   matches_14d,
    }


def load_sackmann_data(all_matches: Optional[List[dict]] = None) -> None:
    """Populate _SACKMANN_PROFILES + _RECENT_STATS from Sackmann CSV data."""
    if all_matches is None:
        all_matches = fetch_sackmann_matches()
    if not all_matches:
        log.warning("load_sackmann_data: no match data — using static stats only")
        return
    all_players = {**ATP_STATS, **WTA_STATS}

    # Also collect players from Sackmann data who are NOT in the static list
    # (e.g. qualifiers, lower-ranked players currently in ATP/WTA draws)
    extra_players: Dict[str, str] = {}   # key → full_name
    for row in all_matches:
        for field in ("winner_name", "loser_name"):
            name = (row.get(field) or "").strip()
            if not name:
                continue
            key = norm_player(name.lower())
            if key not in all_players and key not in extra_players:
                extra_players[key] = name
    ok = 0
    for key, pdata in all_players.items():
        full_name = pdata.get("full_name", "")
        if not full_name:
            continue
        profile = build_player_profile(all_matches, full_name, n=20)
        if profile:
            _SACKMANN_PROFILES[key] = profile
            if profile.get("svpt_won"):
                rec: dict = {"svpt_won": profile["svpt_won"]}
                if profile.get("rtpt_won"):
                    rec["rtpt_won"] = profile["rtpt_won"]
                _RECENT_STATS[key] = rec
            ok += 1
    log.info("load_sackmann_data: %d/%d static players profiled", ok, len(all_players))

    # Profile extra players (qualifiers / lower-ranked not in static list)
    extra_ok = 0
    for key, full_name in extra_players.items():
        profile = build_player_profile(all_matches, full_name, n=20)
        if profile:
            _SACKMANN_PROFILES[key] = profile
            if profile.get("svpt_won"):
                rec2: dict = {"svpt_won": profile["svpt_won"]}
                if profile.get("rtpt_won"):
                    rec2["rtpt_won"] = profile["rtpt_won"]
                _RECENT_STATS[key] = rec2
            extra_ok += 1
    log.info("load_sackmann_data: +%d/%d extra players profiled", extra_ok, len(extra_players))

    # Compute live surface ELO from match history (replaces static fetch_ta_elo)
    compute_elo_from_sackmann(all_matches)


# ─────────────────────────────────────────────────────────────────────────────
# PREDICTION ENGINE  (v3.2 — 15-factor model)
# ─────────────────────────────────────────────────────────────────────────────

def predict(p1_key: str, p2_key: str, surface: str,
            tour_level: str = "atp250", best_of: int = 3,
            sport_key: str = "", tournament: str = "") -> dict:
    """
    Base: 25% Surface ELO + 25% Markov + 20% H/B + 30% Advanced (DF-adjusted H/B)
    Adj:  fatigue(age-weighted) | surface-form | H2H | clutch | DF | lefty | backhand
          altitude | wind | win-streak
    """
    surf_adj = SURFACE_PT_ADJ.get(surface, 0.0)
    is_wta   = "wta" in sport_key.lower() or "wta" in tour_level.lower()

    s1 = get_surface_stats(p1_key, surface)
    s2 = get_surface_stats(p2_key, surface)

    prof1 = _SACKMANN_PROFILES.get(p1_key, {})
    prof2 = _SACKMANN_PROFILES.get(p2_key, {})

    cs_adj   = get_court_speed_adj(sport_key, tournament)
    lefty_sv = lefty_matchup_adj(p1_key, p2_key, surface)

    p1_sv = max(0.50, min(0.78,
        0.5 * (s1["svpt_won"] + 1.0 - s2["rtpt_won"]) + surf_adj + cs_adj + lefty_sv))
    p2_sv = max(0.50, min(0.78,
        0.5 * (s2["svpt_won"] + 1.0 - s1["rtpt_won"]) + surf_adj + cs_adj - lefty_sv))

    markov_p1 = match_win_prob(p1_sv, p2_sv, best_of=best_of)
    elo_p1    = elo_win_prob(s1.get("elo", 1800), s2.get("elo", 1800))

    hold1  = game_win_prob(max(0.50, min(0.80, s1["svpt_won"] + surf_adj + cs_adj)))
    hold2  = game_win_prob(max(0.50, min(0.80, s2["svpt_won"] + surf_adj + cs_adj)))
    break1 = game_win_prob(max(0.30, min(0.65, s1["rtpt_won"] - surf_adj)))
    break2 = game_win_prob(max(0.30, min(0.65, s2["rtpt_won"] - surf_adj)))
    hb_p1  = hold_break_win_prob(hold1, break1, hold2, break2)

    df1 = prof1.get("df_rate") or 0.04
    df2 = prof2.get("df_rate") or 0.04
    hold1_df = game_win_prob(max(0.50, min(0.80,
        s1["svpt_won"] * (1.0 - df1 * 1.5) + surf_adj + cs_adj)))
    hold2_df = game_win_prob(max(0.50, min(0.80,
        s2["svpt_won"] * (1.0 - df2 * 1.5) + surf_adj + cs_adj)))
    adv_p1 = hold_break_win_prob(hold1_df, break1, hold2_df, break2)

    raw_prob = 0.25 * elo_p1 + 0.25 * markov_p1 + 0.20 * hb_p1 + 0.30 * adv_p1

    fat1 = fatigue_score(
        prof1.get("days_rest", 3),
        float(prof1.get("last_minutes", 90)),
        int(prof1.get("last_sets", 3)),
    ) * age_fatigue_mult(p1_key)
    fat2 = fatigue_score(
        prof2.get("days_rest", 3),
        float(prof2.get("last_minutes", 90)),
        int(prof2.get("last_sets", 3)),
    ) * age_fatigue_mult(p2_key)
    fat_adj_val = max(-0.10, min(0.10, (fat2 - fat1) * 0.015))

    form1_rate = prof1.get("form_rate", 0.5)
    form2_rate = prof2.get("form_rate", 0.5)
    global_form = (form1_rate - form2_rate) * 0.15
    surf_form   = surface_form_adj(p1_key, p2_key, surface)
    form_adj_val = max(-0.05, min(0.05, global_form * 0.6 + surf_form * 0.4))

    h2h_val     = h2h_adj(p1_key, p2_key, surface)
    clutch_val  = clutch_adj(p1_key, p2_key)
    df_val      = df_penalty_adj(p1_key, p2_key, is_wta)
    bh_val      = backhand_matchup_adj(p1_key, p2_key, surface)
    streak_val  = win_streak_adj(p1_key, p2_key)
    bo5_val     = bo5_adj(p1_key, p2_key, best_of)
    fs_val      = first_serve_adj(p1_key, p2_key)
    bp_atk_val  = bp_attack_adj(p1_key, p2_key)
    cond_val    = conditioning_adj(p1_key, p2_key)

    alt_adj    = altitude_adj(tournament, surface)
    # altitude shifts serve probability directly (same direction for both)
    p1_sv = max(0.50, min(0.78, p1_sv + alt_adj))
    p2_sv = max(0.50, min(0.78, p2_sv + alt_adj))

    wind_val, wind_kmh = wind_adj(tournament, surface, p1_key, p2_key)

    blend = max(0.05, min(0.95,
        raw_prob + fat_adj_val + form_adj_val + h2h_val + clutch_val
        + df_val + bh_val + streak_val + wind_val
        + bo5_val + fs_val + bp_atk_val + cond_val
    ))

    exp_g = expected_total_games(p1_sv, p2_sv, best_of=best_of)

    log.info(
        "predict %s vs %s [%s%s] ELO=%.3f MC=%.3f HB=%.3f ADV=%.3f raw=%.3f "
        "fat=%+.3f frm=%+.3f h2h=%+.3f clch=%+.3f df=%+.3f bh=%+.3f "
        "streak=%+.3f wind=%+.3f alt=%.3f bo5=%+.3f fs=%+.3f bp=%+.3f cond=%+.3f -> %.3f",
        p1_key, p2_key, surface, " indoor" if cs_adj > 0.01 else "",
        elo_p1, markov_p1, hb_p1, adv_p1, raw_prob,
        fat_adj_val, form_adj_val, h2h_val, clutch_val, df_val, bh_val,
        streak_val, wind_val, alt_adj, bo5_val, fs_val, bp_atk_val, cond_val, blend,
    )

    return {
        "blend_p1":        round(blend, 4),
        "model_p1":        round(markov_p1, 4),
        "elo_p1":          round(elo_p1, 4),
        "hb_p1":           round(hb_p1, 4),
        "adv_p1":          round(adv_p1, 4),
        "h2h_adj":         round(h2h_val, 4),
        "fat_adj":         round(fat_adj_val, 4),
        "form_adj":        round(form_adj_val, 4),
        "clutch_adj":      round(clutch_val, 4),
        "df_adj":          round(df_val, 4),
        "lefty_adj":       round(lefty_sv, 4),
        "backhand_adj":    round(bh_val, 4),
        "p1_sv":           round(p1_sv, 4),
        "p2_sv":           round(p2_sv, 4),
        "elo1":            s1.get("elo", 1800),
        "elo2":            s2.get("elo", 1800),
        "fatigue1":        round(fat1, 1),
        "fatigue2":        round(fat2, 1),
        "form1":           round(form1_rate, 3),
        "form2":           round(form2_rate, 3),
        "tb_win1":         round(prof1.get("tb_win_pct") or 0.5, 3),
        "tb_win2":         round(prof2.get("tb_win_pct") or 0.5, 3),
        "bp_save1":        round(prof1.get("bp_save_pct") or 0.60, 3),
        "bp_save2":        round(prof2.get("bp_save_pct") or 0.60, 3),
        "df_rate1":        round(df1, 4),
        "df_rate2":        round(df2, 4),
        "ace_rate1":       round(prof1.get("ace_rate") or 0.06, 4),
        "ace_rate2":       round(prof2.get("ace_rate") or 0.06, 4),
        "expected_games":  round(exp_g, 1),
        "best_of":         best_of,
        "surface":         surface,
        "court_speed_adj": round(cs_adj, 4),
        "altitude_adj":    round(alt_adj, 4),
        "wind_adj":        round(wind_val, 4),
        "wind_kmh":        round(wind_kmh, 1),
        "streak_adj":      round(streak_val, 4),
        "win_streak1":     prof1.get("win_streak", 0),
        "win_streak2":     prof2.get("win_streak", 0),
        "bo5_adj":         round(bo5_val, 4),
        "fs_adj":          round(fs_val, 4),
        "bp_atk_adj":      round(bp_atk_val, 4),
        "cond_adj":        round(cond_val, 4),
        "bp_conv1":        round(prof1.get("bp_conv_pct") or 0.40, 3),
        "bp_conv2":        round(prof2.get("bp_conv_pct") or 0.40, 3),
        "fs_pct1":         round(prof1.get("first_serve_pct") or 0.60, 3),
        "fs_pct2":         round(prof2.get("first_serve_pct") or 0.60, 3),
        "load1":           prof1.get("matches_last_14d", 0),
        "load2":           prof2.get("matches_last_14d", 0),
        "is_wta":          is_wta,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ODDS FETCHING & PARSING
# ─────────────────────────────────────────────────────────────────────────────
ODDS_SPORTS = [
    "tennis_atp", "tennis_wta",
    "tennis_atp_french_open",      "tennis_wta_french_open",
    "tennis_atp_wimbledon",        "tennis_wta_wimbledon",
    "tennis_atp_us_open",          "tennis_wta_us_open",
    "tennis_atp_australian_open",  "tennis_wta_australian_open",
]


def safe_get(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 404:
            log.debug("safe_get %s: 404 (inactive)", url.split("?")[0])
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("safe_get %s: %s", url, e)
        return None


def fetch_odds() -> List[dict]:
    if not ODDS_API_KEY:
        log.warning("ODDS_API_KEY not set")
        return []
    results = []
    for sport in ODDS_SPORTS:
        data = safe_get(
            "https://api.the-odds-api.com/v4/sports/%s/odds/" % sport,
            params={"apiKey": ODDS_API_KEY, "regions": "us,eu,uk,au",
                    "markets": "h2h", "oddsFormat": "decimal"},
        )
        if data:
            for g in data:
                g["_sport"] = sport
            results.extend(data)
            log.info("  %s: %d games", sport, len(data))
    return results


def devigge(p1_raw: float, p2_raw: float) -> float:
    total = p1_raw + p2_raw
    return p1_raw / total if total > 1e-6 else 0.5


def parse_odds(raw: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for game in raw:
        home  = game.get("home_team", "")
        away  = game.get("away_team", "")
        books = game.get("bookmakers", [])
        if not home or not away or not books:
            continue
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
        if len(hp) < MIN_BOOKS or len(ap) < MIN_BOOKS:
            continue
        best_h = max(hp); best_a = max(ap)
        cons_h = sum(hp) / len(hp); cons_a = sum(ap) / len(ap)
        dv_h   = devigge(1.0 / cons_h, 1.0 / cons_a)
        key    = "%s|%s" % (home.lower(), away.lower())
        out[key] = {
            "home": home, "away": away,
            "best_home": round(best_h, 3), "best_away": round(best_a, 3),
            "dv_p_home": round(dv_h, 4),  "dv_p_away": round(1.0 - dv_h, 4),
            "n_books":     len(hp),
            "sport":       game.get("_sport", ""),
            "sport_title": game.get("sport_title", ""),
            "commence":    game.get("commence_time", ""),
        }
    log.info("Parsed odds for %d matches", len(out))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LIVE ELO FROM TENNIS ABSTRACT
# ─────────────────────────────────────────────────────────────────────────────

def fetch_ta_elo() -> None:
    try:
        r = requests.get(
            "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_players.csv",
            timeout=20)
        r.raise_for_status()
        reader  = csv.DictReader(io.StringIO(r.text))
        updated = 0
        for row in reader:
            name = ("%s %s" % (row.get("name_first", ""),
                               row.get("name_last", ""))).strip().lower()
            key  = norm_player(name)
            if key not in ATP_STATS:
                continue
            elo_str = row.get("elo") or row.get("elo_rating") or ""
            if not elo_str:
                continue
            try:
                elo = float(elo_str)
                _LIVE_ELO[key] = {
                    "hard":  elo,
                    "clay":  elo - 30,
                    "grass": elo + 10,
                }
                updated += 1
            except ValueError:
                pass
        log.info("fetch_ta_elo: updated %d players", updated)
    except Exception as e:
        log.warning("fetch_ta_elo: %s — using baseline ELO", e)


# ─────────────────────────────────────────────────────────────────────────────
# KELLY CRITERION & PICK GENERATION
# ─────────────────────────────────────────────────────────────────────────────

def kelly_stake(model_p: float, price: float, conf: float = 1.0) -> float:
    if price <= 1.0 or model_p <= 0.0:
        return 0.0
    ev = model_p * price - 1.0
    if ev <= 0:
        return 0.0
    kf    = (model_p * price - 1.0) / (price - 1.0)
    stake = BANKROLL * kf * KELLY * conf
    return max(KELLY_FLOOR, min(KELLY_MAX, stake))


def generate_picks(matches: List[dict],
                   odds_prev: Optional[Dict[str, dict]] = None) -> List[dict]:
    picks     = []
    daily_exp = 0.0
    if odds_prev is None:
        odds_prev = {}

    for m in matches:
        if daily_exp >= MAX_DAILY_EXP:
            break
        pred      = m["pred"]
        odds_info = m["odds_info"]
        p1_key    = m["p1_key"]
        p2_key    = m["p2_key"]

        blend_p1 = pred["blend_p1"]
        blend_p2 = 1.0 - blend_p1
        dv_p1    = odds_info["dv_p_home"]
        dv_p2    = odds_info["dv_p_away"]

        # Odds movement signal (smart-money boost)
        ok = "%s|%s" % (odds_info["home"].lower(), odds_info["away"].lower())
        steam_adj, steam_label = odds_move_signal(ok, odds_info, odds_prev)
        blend_p1 = max(0.05, min(0.95, blend_p1 + steam_adj))
        blend_p2 = 1.0 - blend_p1

        edge1 = blend_p1 - dv_p1
        edge2 = blend_p2 - dv_p2

        if edge1 >= edge2:
            edge, model_p, dv_p = edge1, blend_p1, dv_p1
            best_price, bet_name = odds_info["best_home"], odds_info["home"]
        else:
            edge, model_p, dv_p = edge2, blend_p2, dv_p2
            best_price, bet_name = odds_info["best_away"], odds_info["away"]

        # Market efficiency: more bookmakers → tighter market → require higher edge
        n_books = odds_info.get("n_books", MIN_BOOKS)
        eff_min_edge = MIN_EDGE_ML + max(0.0, (n_books - 6) * 0.003)
        # 高賠率安全閥：推薦的賠率 > 2.20 時要求更高 edge，避免模型噪音放大
        if best_price > 2.20:
            eff_min_edge = max(eff_min_edge, 0.15)
        # 市場機率底線：市場認為我們推薦的選手勝率 < 28% → 模型可能誤判，跳過
        if dv_p < 0.28:
            continue

        if edge < eff_min_edge:
            continue
        if model_p < MIN_CONF_ML:
            continue
        if p1_key in _INJURIES or p2_key in _INJURIES:
            continue

        # Tier classification before Kelly so we can use tier-specific fraction
        if edge >= 0.12:
            star = "\U0001f48e"; tier = "A"
        elif edge >= 0.09:
            star = "⭐"; tier = "B"
        else:
            star = "•"; tier = "C"

        conf         = min(1.0, (model_p - MIN_CONF_ML) * 2.0 + 0.70)
        kelly_frac   = KELLY_BY_TIER.get(tier, KELLY)
        kf_raw       = (model_p * best_price - 1.0) / (best_price - 1.0) if best_price > 1.0 else 0.0
        stake        = max(KELLY_FLOOR, min(KELLY_MAX, BANKROLL * kf_raw * kelly_frac * conf))
        stake        = min(stake, MAX_DAILY_EXP - daily_exp)
        daily_exp   += stake

        surface_emoji = {"clay": "\U0001f7e4", "grass": "\U0001f7e2", "hard": "\U0001f535"}.get(m["surface"], "⚪")

        picks.append({
            "tier":           tier,
            "star":           star,
            "surface_emoji":  surface_emoji,
            "tour":           TOUR_META.get(m["tour_level"], {}).get("name", m["tour_level"]),
            "tour_level":     m["tour_level"],
            "tour_type":      "WTA" if pred.get("is_wta") else "ATP",
            "tournament":     m.get("tournament", ""),
            "sport_title":    m.get("sport_title", ""),
            "surface":        m["surface"],
            "p1":             odds_info["home"],
            "p2":             odds_info["away"],
            "p1_cn":          cn_name(odds_info["home"]),
            "p2_cn":          cn_name(odds_info["away"]),
            "p1_key":         p1_key,
            "p2_key":         p2_key,
            "bet_on":         bet_name,
            "bet_on_cn":      cn_name(bet_name),
            "best_price":     round(best_price, 3),
            "model_p":        round(model_p * 100, 1),
            "dv_p":           round(dv_p * 100, 1),
            "edge":           round(edge * 100, 1),
            "conf":           round(conf * 100, 1),
            "stake":          round(stake, 0),
            "p1_sv_pct":      round(pred["p1_sv"] * 100, 1),
            "p2_sv_pct":      round(pred["p2_sv"] * 100, 1),
            "elo1":           pred["elo1"],
            "elo2":           pred["elo2"],
            "expected_games": pred["expected_games"],
            "best_of":        pred["best_of"],
            "h2h_adj":        round(pred["h2h_adj"] * 100, 1),
            "hb_p1":          round(pred.get("hb_p1", 0.5) * 100, 1),
            "form_adj":       round(pred.get("form_adj", 0.0) * 100, 1),
            "fat_adj":        round(pred.get("fat_adj", 0.0) * 100, 1),
            "fatigue1":       pred.get("fatigue1", 0.0),
            "fatigue2":       pred.get("fatigue2", 0.0),
            "form1":          round(pred.get("form1", 0.5) * 100, 1),
            "form2":          round(pred.get("form2", 0.5) * 100, 1),
            "commence":       odds_info.get("commence", ""),
            "adv_p1":         round(pred.get("adv_p1", 0.5) * 100, 1),
            "clutch_adj":     round(pred.get("clutch_adj", 0.0) * 100, 1),
            "df_adj":         round(pred.get("df_adj", 0.0) * 100, 1),
            "lefty_adj":      round(pred.get("lefty_adj", 0.0) * 100, 1),
            "backhand_adj":   round(pred.get("backhand_adj", 0.0) * 100, 1),
            "tb_win1":        round(pred.get("tb_win1", 0.5) * 100, 1),
            "tb_win2":        round(pred.get("tb_win2", 0.5) * 100, 1),
            "bp_save1":       round(pred.get("bp_save1", 0.6) * 100, 1),
            "bp_save2":       round(pred.get("bp_save2", 0.6) * 100, 1),
            "df_rate1":       round(pred.get("df_rate1", 0.04) * 100, 2),
            "df_rate2":       round(pred.get("df_rate2", 0.04) * 100, 2),
            "ace_rate1":      round(pred.get("ace_rate1", 0.06) * 100, 2),
            "ace_rate2":      round(pred.get("ace_rate2", 0.06) * 100, 2),
            "court_speed":    round(pred.get("court_speed_adj", 0.0) * 100, 2),
            "altitude_adj":   round(pred.get("altitude_adj", 0.0) * 100, 2),
            "wind_adj":       round(pred.get("wind_adj", 0.0) * 100, 2),
            "wind_kmh":       pred.get("wind_kmh", 0.0),
            "streak_adj":     round(pred.get("streak_adj", 0.0) * 100, 2),
            "win_streak1":    pred.get("win_streak1", 0),
            "win_streak2":    pred.get("win_streak2", 0),
            "bo5_adj":        round(pred.get("bo5_adj", 0.0) * 100, 2),
            "fs_adj":         round(pred.get("fs_adj", 0.0) * 100, 2),
            "bp_atk_adj":     round(pred.get("bp_atk_adj", 0.0) * 100, 2),
            "cond_adj":       round(pred.get("cond_adj", 0.0) * 100, 2),
            "bp_conv1":       round(pred.get("bp_conv1", 0.40) * 100, 1),
            "bp_conv2":       round(pred.get("bp_conv2", 0.40) * 100, 1),
            "fs_pct1":        round(pred.get("fs_pct1", 0.60) * 100, 1),
            "fs_pct2":        round(pred.get("fs_pct2", 0.60) * 100, 1),
            "load1":          pred.get("load1", 0),
            "load2":          pred.get("load2", 0),
            "steam":          steam_label,
            "n_books":        n_books,
            "eff_min_edge":   round(eff_min_edge * 100, 1),
        })
        log.info("  PICK %s %s vs %s -> %s @%.2f model=%.1f%% edge=+%.1f%% $%.0f",
                 star, odds_info["home"], odds_info["away"],
                 bet_name, best_price, model_p * 100, edge * 100, stake)

    picks.sort(key=lambda x: -x["edge"])
    return picks[:MAX_PICKS]


# ─────────────────────────────────────────────────────────────────────────────
# HISTORY (GitHub Gist)
# ─────────────────────────────────────────────────────────────────────────────

def load_history() -> dict:
    if not GIST_TOKEN or not GIST_ID:
        return {"bets": []}
    data = safe_get("https://api.github.com/gists/%s" % GIST_ID)
    if not data:
        return {"bets": []}
    for fname, fd in data.get("files", {}).items():
        if fname.endswith(".json"):
            try:
                return json.loads(fd.get("content", "{}"))
            except json.JSONDecodeError:
                pass
    return {"bets": []}


def save_history(hist: dict) -> None:
    if not GIST_TOKEN or not GIST_ID:
        return
    try:
        requests.patch(
            "https://api.github.com/gists/%s" % GIST_ID,
            headers={"Authorization": "token %s" % GIST_TOKEN},
            json={"files": {"tennis_hist.json": {
                "content": json.dumps(hist, ensure_ascii=False, indent=2)}}},
            timeout=15,
        )
    except Exception as e:
        log.warning("save_history: %s", e)


# Regional opening times (TW, UTC+8) — record window = 40 min before each
# AUS/Asia: 07:00, Europe: 17:00, Americas: 23:00
_RECORD_WINDOWS: List[Tuple[int, int]] = [
    (6, 20),   # 06:20–07:00 → AUS/Asia opens 07:00
    (16, 20),  # 16:20–17:00 → Europe opens 17:00
    (22, 20),  # 22:20–23:00 → Americas opens 23:00
]


def in_recording_window(now_tw: datetime.datetime) -> bool:
    """Return True if current TW time is within 40 min before a regional open."""
    h, m = now_tw.hour, now_tw.minute
    cur = h * 60 + m
    for wh, wm in _RECORD_WINDOWS:
        win_start = wh * 60 + wm
        win_end   = win_start + 40
        if win_start <= cur < win_end:
            return True
    return False


_SLAM_KEYS = {"french_open", "wimbledon", "us_open", "australian_open"}


def filter_slam_picks(picks: List[dict]) -> List[dict]:
    """Return only picks belonging to the currently active Grand Slam."""
    slam_picks = [p for p in picks if p.get("tournament", "") in _SLAM_KEYS]
    if not slam_picks:
        return []
    # Determine which slam is dominant (most picks) and keep only that slam
    from collections import Counter
    dominant = Counter(p["tournament"] for p in slam_picks).most_common(1)[0][0]
    return [p for p in slam_picks if p["tournament"] == dominant]


def record_picks_to_history(picks: List[dict], hist: dict,
                             now_tw: datetime.datetime) -> None:
    """Append today's picks as pending bets if not already recorded."""
    bets   = hist.setdefault("bets", [])
    today  = now_tw.strftime("%Y-%m-%d")
    # Deduplicate: skip picks already recorded for same matchup today
    existing = {
        (b["p1"], b["p2"], b["date"])
        for b in bets
        if "p1" in b and "p2" in b
    }
    added = 0
    for p in picks:
        key = (p["p1"], p["p2"], today)
        if key in existing:
            continue
        bets.append({
            "date":     today,
            "p1":       p["p1"],
            "p2":       p["p2"],
            "bet_on":   p["bet_on"],
            "price":    p["best_price"],
            "stake":    p["stake"],
            "edge":     p["edge"],
            "tier":     p["tier"],
            "surface":  p["surface"],
            "tour":     p["tour"],
            "result":   "P",   # pending — update manually or via result bot
        })
        existing.add(key)
        added += 1
    log.info("record_picks_to_history: +%d new bets (total %d)", added, len(bets))


def compute_stats(hist: dict) -> dict:
    bets = [b for b in hist.get("bets", []) if b.get("result") in ("W", "L")]
    if not bets:
        return {"settled": 0, "wins": 0, "win_rate": 0.0, "roi": 0.0, "pnl": 0.0}
    wins     = sum(1 for b in bets if b["result"] == "W")
    total_in = sum(b.get("stake", 100) for b in bets)
    pnl      = sum(
        b.get("stake", 100) * (b.get("price", 2.0) - 1) if b["result"] == "W"
        else -b.get("stake", 100)
        for b in bets
    )
    return {
        "settled":  len(bets), "wins": wins,
        "win_rate": round(wins / len(bets) * 100, 1),
        "pnl":      round(pnl, 1),
        "roi":      round(pnl / total_in * 100, 1) if total_in > 0 else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────────────────

def send_ntfy(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        return
    try:
        requests.post("https://ntfy.sh",
                      json={"topic": NTFY_TOPIC, "title": title,
                            "message": message, "priority": 4, "tags": ["tennis"]},
                      timeout=10)
    except Exception as e:
        log.warning("ntfy: %s", e)


def send_discord(picks: List[dict], stats: dict) -> None:
    if not DISCORD_HOOK:
        return
    now   = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    lines = ["**\U0001f3be ATP/WTA 每日預測 — %s**" % now.strftime("%Y-%m-%d %H:%M"), "```"]
    if not picks:
        lines.append("今日無符合條件的推薦")
    else:
        for p in picks:
            p1d = p.get("p1_cn") or p["p1"]
            p2d = p.get("p2_cn") or p["p2"]
            bnd = p.get("bet_on_cn") or p["bet_on"]
            lines.append("%s %s [%s] %s vs %s" % (
                p["star"], p["surface_emoji"], p.get("tour_type", "ATP"), p1d, p2d))
            lines.append("  推薦: %s @%.2f  模型:%.1f%%  edge:+%.1f%%  $%.0f" % (
                bnd, p["best_price"], p["model_p"], p["edge"], p["stake"]))
            parts = []
            adj_parts = []
            if p.get("fat_adj"):      adj_parts.append("體能:%+.1f%%" % p["fat_adj"])
            if p.get("form_adj"):     adj_parts.append("狀態:%+.1f%%" % p["form_adj"])
            if p.get("clutch_adj"):   adj_parts.append("心理:%+.1f%%" % p["clutch_adj"])
            if p.get("df_adj"):       adj_parts.append("雙誤:%+.1f%%" % p["df_adj"])
            if p.get("lefty_adj"):    adj_parts.append("左手:%+.1f%%" % p["lefty_adj"])
            if p.get("streak_adj"):   adj_parts.append("連勝:%+.1f%%" % p["streak_adj"])
            if p.get("bo5_adj"):      adj_parts.append("BO5:%+.1f%%" % p["bo5_adj"])
            if p.get("bp_atk_adj"):   adj_parts.append("破發攻:%+.1f%%" % p["bp_atk_adj"])
            if p.get("cond_adj"):     adj_parts.append("負荷:%+.1f%%" % p["cond_adj"])
            if p.get("wind_kmh", 0) > 15: adj_parts.append("風:%.0fkm/h" % p["wind_kmh"])
            if p.get("steam"):        adj_parts.append("💰%s" % p["steam"].replace("_"," "))
            if adj_parts:
                lines.append("  " + "  ".join(adj_parts))
            lines.append("  一發:%.0f%%/%.0f%%  破發轉換:%.0f%%/%.0f%%  負荷:%d/%d場" % (
                p.get("fs_pct1", 60), p.get("fs_pct2", 60),
                p.get("bp_conv1", 40), p.get("bp_conv2", 40),
                p.get("load1", 0),    p.get("load2", 0)))
    lines.append("```")
    if stats.get("settled", 0):
        lines.append("戰績: %d/%d (%.1f%%)  ROI: %.1f%%" % (
            stats["wins"], stats["settled"], stats["win_rate"], stats["roi"]))
    try:
        requests.post(DISCORD_HOOK, json={"content": "\n".join(lines)}, timeout=10)
    except Exception as e:
        log.warning("discord: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def write_json(picks: List[dict], stats: dict, history: dict,
               game_preds: dict, now: datetime.datetime) -> None:
    os.makedirs("docs", exist_ok=True)
    payload = {
        "generated_at":     now.strftime("%Y-%m-%d %H:%M") + " (台灣時間)",
        "generated_at_iso": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "date":             now.strftime("%Y-%m-%d"),
        "model_version": "v3.2 — 15-factor: ELO(live)+BO5+SurfaceH2H+1stSrv+BPconv+Cond+KellyTier",
        "stats":         stats,
        "picks":         picks,
        "recent_history": list(reversed(history.get("bets", [])[-10:])),
        "live_matches":  [],
        "game_preds":    game_preds,
    }
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("Wrote %s (%d picks)", JSON_PATH, len(picks))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    now_tw = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    log.info("=== Tennis Bot v3.2 start %s ===", now_tw.strftime("%Y-%m-%d %H:%M"))

    all_matches_raw = fetch_sackmann_matches()
    # ELO is computed from match history inside load_sackmann_data → compute_elo_from_sackmann

    load_sackmann_data(all_matches_raw)

    # Auto-detect retired / injured players from recent Sackmann data
    auto_inj = detect_injuries(all_matches_raw) if all_matches_raw else set()
    _INJURIES.update(auto_inj)
    if auto_inj:
        log.info("Auto-flagged injuries/retirements: %s", auto_inj)

    # Load previous odds snapshot for steam detection
    odds_prev = load_odds_prev()

    raw_odds = fetch_odds()
    odds_map = parse_odds(raw_odds)

    # Save current odds for next-run comparison
    save_odds_prev(odds_map)

    matches:    List[dict]      = []
    game_preds: Dict[str, dict] = {}

    for key, odds_info in odds_map.items():
        sport      = odds_info.get("sport", "tennis_atp")
        surface    = infer_surface(sport)
        t_lvl      = infer_tour_level(sport)
        best_of    = TOUR_META.get(t_lvl, {}).get("best_of", 3)
        tournament = extract_tournament(sport, odds_info)

        p1_key = norm_player(odds_info["home"])
        p2_key = norm_player(odds_info["away"])

        pred = predict(p1_key, p2_key, surface, t_lvl, best_of,
                       sport_key=sport, tournament=tournament)

        game_preds[key] = {
            "p1": odds_info["home"], "p2": odds_info["away"],
            "p1_key": p1_key, "p2_key": p2_key,
            "model_p1":   pred["blend_p1"],
            "surface":    surface,
            "tour_level": t_lvl,
            "best_of":    best_of,
            "exp_games":  pred["expected_games"],
        }
        matches.append({
            "p1_key": p1_key, "p2_key": p2_key,
            "surface": surface, "tour_level": t_lvl, "best_of": best_of,
            "odds_info": odds_info, "pred": pred,
            "tournament": tournament,
            "sport_title": odds_info.get("sport_title", ""),
        })

    log.info("Processed %d matches", len(matches))

    picks   = generate_picks(matches, odds_prev=odds_prev)
    history = load_history()

    # Only record Grand Slam picks to Gist, within 40 min before open time
    if picks and in_recording_window(now_tw):
        slam_picks = filter_slam_picks(picks)
        if slam_picks:
            log.info("Inside recording window — saving %d Grand Slam picks to Gist",
                     len(slam_picks))
            record_picks_to_history(slam_picks, history, now_tw)
            save_history(history)
        else:
            log.info("Inside recording window but no Grand Slam picks — skip Gist write")
    else:
        log.info("Outside recording window (TW %s) — skip Gist write",
                 now_tw.strftime("%H:%M"))

    stats   = compute_stats(history)
    write_json(picks, stats, history, game_preds, now_tw)

    if picks:
        send_ntfy(
            "\U0001f3be Tennis Picks — %s" % now_tw.strftime("%m/%d"),
            "%d 個推薦\n" % len(picks) +
            "\n".join("• %s vs %s → %s @%.2f (+%.1f%%)" % (
                p["p1"], p["p2"], p["bet_on"], p["best_price"], p["edge"]
            ) for p in picks),
        )
    send_discord(picks, stats)
    log.info("=== Done — %d picks ===", len(picks))


if __name__ == "__main__":
    run()
