#!/usr/bin/env python3
"""
Tennis Bot v2.0 — ATP/WTA 巡迴賽預測系統
6因子模型：Surface ELO 35% + Markov Chain 35% + Hold/Break 30%
附加調整：體能 ±8% | 近期狀態 ±5% | H2H ±5%
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
# ATP PLAYER DATABASE
# svpt_won : P(server wins a point when THIS player is serving)
# rtpt_won : P(THIS player wins a return point vs any server)
# elo      : surface-specific Elo rating
# ─────────────────────────────────────────────────────────────────────────────
ATP_STATS: Dict[str, dict] = {
    "djokovic": {
        "full_name": "Novak Djokovic", "hand": "R", "rank": 2, "country": "SRB",
        "hard":  {"svpt_won": 0.663, "rtpt_won": 0.388, "elo": 2375},
        "clay":  {"svpt_won": 0.652, "rtpt_won": 0.392, "elo": 2420},
        "grass": {"svpt_won": 0.671, "rtpt_won": 0.385, "elo": 2355},
    },
    "alcaraz": {
        "full_name": "Carlos Alcaraz", "hand": "R", "rank": 1, "country": "ESP",
        "hard":  {"svpt_won": 0.658, "rtpt_won": 0.382, "elo": 2300},
        "clay":  {"svpt_won": 0.660, "rtpt_won": 0.390, "elo": 2340},
        "grass": {"svpt_won": 0.670, "rtpt_won": 0.378, "elo": 2285},
    },
    "sinner": {
        "full_name": "Jannik Sinner", "hand": "R", "rank": 1, "country": "ITA",
        "hard":  {"svpt_won": 0.665, "rtpt_won": 0.383, "elo": 2310},
        "clay":  {"svpt_won": 0.655, "rtpt_won": 0.375, "elo": 2265},
        "grass": {"svpt_won": 0.668, "rtpt_won": 0.372, "elo": 2250},
    },
    "medvedev": {
        "full_name": "Daniil Medvedev", "hand": "R", "rank": 5, "country": "RUS",
        "hard":  {"svpt_won": 0.662, "rtpt_won": 0.375, "elo": 2240},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.345, "elo": 2085},
        "grass": {"svpt_won": 0.660, "rtpt_won": 0.355, "elo": 2145},
    },
    "zverev": {
        "full_name": "Alexander Zverev", "hand": "R", "rank": 3, "country": "GER",
        "hard":  {"svpt_won": 0.650, "rtpt_won": 0.360, "elo": 2200},
        "clay":  {"svpt_won": 0.648, "rtpt_won": 0.365, "elo": 2215},
        "grass": {"svpt_won": 0.655, "rtpt_won": 0.352, "elo": 2160},
    },
    "rublev": {
        "full_name": "Andrey Rublev", "hand": "R", "rank": 7, "country": "RUS",
        "hard":  {"svpt_won": 0.635, "rtpt_won": 0.355, "elo": 2120},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.360, "elo": 2140},
        "grass": {"svpt_won": 0.638, "rtpt_won": 0.345, "elo": 2080},
    },
    "tsitsipas": {
        "full_name": "Stefanos Tsitsipas", "hand": "R", "rank": 11, "country": "GRE",
        "hard":  {"svpt_won": 0.638, "rtpt_won": 0.358, "elo": 2110},
        "clay":  {"svpt_won": 0.648, "rtpt_won": 0.370, "elo": 2175},
        "grass": {"svpt_won": 0.645, "rtpt_won": 0.348, "elo": 2065},
    },
    "fritz": {
        "full_name": "Taylor Fritz", "hand": "R", "rank": 4, "country": "USA",
        "hard":  {"svpt_won": 0.660, "rtpt_won": 0.358, "elo": 2155},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.338, "elo": 2020},
        "grass": {"svpt_won": 0.668, "rtpt_won": 0.355, "elo": 2120},
    },
    "de_minaur": {
        "full_name": "Alex de Minaur", "hand": "R", "rank": 9, "country": "AUS",
        "hard":  {"svpt_won": 0.635, "rtpt_won": 0.368, "elo": 2100},
        "clay":  {"svpt_won": 0.628, "rtpt_won": 0.365, "elo": 2070},
        "grass": {"svpt_won": 0.640, "rtpt_won": 0.365, "elo": 2085},
    },
    "hurkacz": {
        "full_name": "Hubert Hurkacz", "hand": "R", "rank": 10, "country": "POL",
        "hard":  {"svpt_won": 0.665, "rtpt_won": 0.348, "elo": 2095},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.325, "elo": 1960},
        "grass": {"svpt_won": 0.678, "rtpt_won": 0.345, "elo": 2110},
    },
    "dimitrov": {
        "full_name": "Grigor Dimitrov", "hand": "R", "rank": 13, "country": "BUL",
        "hard":  {"svpt_won": 0.645, "rtpt_won": 0.355, "elo": 2060},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.348, "elo": 2020},
        "grass": {"svpt_won": 0.652, "rtpt_won": 0.352, "elo": 2045},
    },
    "paul": {
        "full_name": "Tommy Paul", "hand": "R", "rank": 12, "country": "USA",
        "hard":  {"svpt_won": 0.640, "rtpt_won": 0.355, "elo": 2040},
        "clay":  {"svpt_won": 0.632, "rtpt_won": 0.345, "elo": 2005},
        "grass": {"svpt_won": 0.645, "rtpt_won": 0.348, "elo": 2025},
    },
    "auger_aliassime": {
        "full_name": "Felix Auger-Aliassime", "hand": "R", "rank": 20, "country": "CAN",
        "hard":  {"svpt_won": 0.655, "rtpt_won": 0.352, "elo": 2035},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.338, "elo": 1980},
        "grass": {"svpt_won": 0.662, "rtpt_won": 0.348, "elo": 2020},
    },
    "musetti": {
        "full_name": "Lorenzo Musetti", "hand": "L", "rank": 16, "country": "ITA",
        "hard":  {"svpt_won": 0.625, "rtpt_won": 0.348, "elo": 2010},
        "clay":  {"svpt_won": 0.635, "rtpt_won": 0.358, "elo": 2055},
        "grass": {"svpt_won": 0.638, "rtpt_won": 0.348, "elo": 2035},
    },
    "tiafoe": {
        "full_name": "Frances Tiafoe", "hand": "R", "rank": 15, "country": "USA",
        "hard":  {"svpt_won": 0.638, "rtpt_won": 0.352, "elo": 2025},
        "clay":  {"svpt_won": 0.620, "rtpt_won": 0.335, "elo": 1950},
        "grass": {"svpt_won": 0.648, "rtpt_won": 0.345, "elo": 1985},
    },
    "berrettini": {
        "full_name": "Matteo Berrettini", "hand": "R", "rank": 35, "country": "ITA",
        "hard":  {"svpt_won": 0.658, "rtpt_won": 0.345, "elo": 2050},
        "clay":  {"svpt_won": 0.648, "rtpt_won": 0.342, "elo": 2015},
        "grass": {"svpt_won": 0.680, "rtpt_won": 0.345, "elo": 2085},
    },
    "ruud": {
        "full_name": "Casper Ruud", "hand": "R", "rank": 14, "country": "NOR",
        "hard":  {"svpt_won": 0.630, "rtpt_won": 0.348, "elo": 2025},
        "clay":  {"svpt_won": 0.645, "rtpt_won": 0.362, "elo": 2095},
        "grass": {"svpt_won": 0.628, "rtpt_won": 0.332, "elo": 1945},
    },
    "draper": {
        "full_name": "Jack Draper", "hand": "L", "rank": 17, "country": "GBR",
        "hard":  {"svpt_won": 0.648, "rtpt_won": 0.355, "elo": 2020},
        "clay":  {"svpt_won": 0.638, "rtpt_won": 0.348, "elo": 1985},
        "grass": {"svpt_won": 0.655, "rtpt_won": 0.352, "elo": 2030},
    },
    "shelton": {
        "full_name": "Ben Shelton", "hand": "L", "rank": 21, "country": "USA",
        "hard":  {"svpt_won": 0.658, "rtpt_won": 0.348, "elo": 2000},
        "clay":  {"svpt_won": 0.628, "rtpt_won": 0.325, "elo": 1890},
        "grass": {"svpt_won": 0.668, "rtpt_won": 0.340, "elo": 1985},
    },
    "khachanov": {
        "full_name": "Karen Khachanov", "hand": "R", "rank": 22, "country": "RUS",
        "hard":  {"svpt_won": 0.645, "rtpt_won": 0.345, "elo": 2000},
        "clay":  {"svpt_won": 0.635, "rtpt_won": 0.338, "elo": 1975},
        "grass": {"svpt_won": 0.650, "rtpt_won": 0.335, "elo": 1975},
    },
    "bublik": {
        "full_name": "Alexander Bublik", "hand": "R", "rank": 24, "country": "KAZ",
        "hard":  {"svpt_won": 0.658, "rtpt_won": 0.328, "elo": 1955},
        "clay":  {"svpt_won": 0.635, "rtpt_won": 0.312, "elo": 1880},
        "grass": {"svpt_won": 0.668, "rtpt_won": 0.322, "elo": 1965},
    },
    "humbert": {
        "full_name": "Ugo Humbert", "hand": "L", "rank": 19, "country": "FRA",
        "hard":  {"svpt_won": 0.648, "rtpt_won": 0.355, "elo": 2005},
        "clay":  {"svpt_won": 0.632, "rtpt_won": 0.342, "elo": 1945},
        "grass": {"svpt_won": 0.655, "rtpt_won": 0.348, "elo": 1990},
    },
    "jarry": {
        "full_name": "Nicolas Jarry", "hand": "R", "rank": 28, "country": "CHI",
        "hard":  {"svpt_won": 0.648, "rtpt_won": 0.332, "elo": 1935},
        "clay":  {"svpt_won": 0.645, "rtpt_won": 0.338, "elo": 1955},
        "grass": {"svpt_won": 0.645, "rtpt_won": 0.325, "elo": 1905},
    },
    "cobolli": {
        "full_name": "Flavio Cobolli", "hand": "R", "rank": 30, "country": "ITA",
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
        "hard":  {"svpt_won": 0.580, "rtpt_won": 0.440, "elo": 2250},
        "clay":  {"svpt_won": 0.590, "rtpt_won": 0.455, "elo": 2355},
        "grass": {"svpt_won": 0.568, "rtpt_won": 0.418, "elo": 2120},
    },
    "sabalenka": {
        "full_name": "Aryna Sabalenka", "hand": "R", "rank": 1, "country": "BLR",
        "hard":  {"svpt_won": 0.598, "rtpt_won": 0.418, "elo": 2215},
        "clay":  {"svpt_won": 0.582, "rtpt_won": 0.408, "elo": 2120},
        "grass": {"svpt_won": 0.595, "rtpt_won": 0.405, "elo": 2145},
    },
    "gauff": {
        "full_name": "Coco Gauff", "hand": "R", "rank": 3, "country": "USA",
        "hard":  {"svpt_won": 0.578, "rtpt_won": 0.415, "elo": 2125},
        "clay":  {"svpt_won": 0.572, "rtpt_won": 0.412, "elo": 2090},
        "grass": {"svpt_won": 0.565, "rtpt_won": 0.400, "elo": 2055},
    },
    "rybakina": {
        "full_name": "Elena Rybakina", "hand": "R", "rank": 7, "country": "KAZ",
        "hard":  {"svpt_won": 0.595, "rtpt_won": 0.408, "elo": 2155},
        "clay":  {"svpt_won": 0.578, "rtpt_won": 0.398, "elo": 2075},
        "grass": {"svpt_won": 0.605, "rtpt_won": 0.408, "elo": 2175},
    },
    "pegula": {
        "full_name": "Jessica Pegula", "hand": "R", "rank": 6, "country": "USA",
        "hard":  {"svpt_won": 0.572, "rtpt_won": 0.405, "elo": 2070},
        "clay":  {"svpt_won": 0.558, "rtpt_won": 0.392, "elo": 1985},
        "grass": {"svpt_won": 0.560, "rtpt_won": 0.388, "elo": 1985},
    },
    "keys": {
        "full_name": "Madison Keys", "hand": "R", "rank": 5, "country": "USA",
        "hard":  {"svpt_won": 0.582, "rtpt_won": 0.395, "elo": 2065},
        "clay":  {"svpt_won": 0.565, "rtpt_won": 0.378, "elo": 1985},
        "grass": {"svpt_won": 0.580, "rtpt_won": 0.380, "elo": 2020},
    },
    "zheng": {
        "full_name": "Qinwen Zheng", "hand": "R", "rank": 8, "country": "CHN",
        "hard":  {"svpt_won": 0.575, "rtpt_won": 0.400, "elo": 2060},
        "clay":  {"svpt_won": 0.568, "rtpt_won": 0.395, "elo": 2035},
        "grass": {"svpt_won": 0.565, "rtpt_won": 0.385, "elo": 2005},
    },
    "paolini": {
        "full_name": "Jasmine Paolini", "hand": "R", "rank": 4, "country": "ITA",
        "hard":  {"svpt_won": 0.562, "rtpt_won": 0.402, "elo": 2050},
        "clay":  {"svpt_won": 0.568, "rtpt_won": 0.410, "elo": 2090},
        "grass": {"svpt_won": 0.555, "rtpt_won": 0.388, "elo": 2010},
    },
    "navarro": {
        "full_name": "Emma Navarro", "hand": "R", "rank": 9, "country": "USA",
        "hard":  {"svpt_won": 0.562, "rtpt_won": 0.395, "elo": 2020},
        "clay":  {"svpt_won": 0.552, "rtpt_won": 0.382, "elo": 1965},
        "grass": {"svpt_won": 0.568, "rtpt_won": 0.392, "elo": 2025},
    },
    "krejcikova": {
        "full_name": "Barbora Krejcikova", "hand": "R", "rank": 10, "country": "CZE",
        "hard":  {"svpt_won": 0.555, "rtpt_won": 0.388, "elo": 1975},
        "clay":  {"svpt_won": 0.565, "rtpt_won": 0.400, "elo": 2025},
        "grass": {"svpt_won": 0.568, "rtpt_won": 0.395, "elo": 2030},
    },
    "sakkari": {
        "full_name": "Maria Sakkari", "hand": "R", "rank": 12, "country": "GRE",
        "hard":  {"svpt_won": 0.562, "rtpt_won": 0.385, "elo": 2000},
        "clay":  {"svpt_won": 0.558, "rtpt_won": 0.382, "elo": 1990},
        "grass": {"svpt_won": 0.555, "rtpt_won": 0.370, "elo": 1955},
    },
    "kasatkina": {
        "full_name": "Daria Kasatkina", "hand": "R", "rank": 15, "country": "RUS",
        "hard":  {"svpt_won": 0.555, "rtpt_won": 0.388, "elo": 1975},
        "clay":  {"svpt_won": 0.562, "rtpt_won": 0.395, "elo": 2005},
        "grass": {"svpt_won": 0.548, "rtpt_won": 0.375, "elo": 1935},
    },
    "kvitova": {
        "full_name": "Petra Kvitova", "hand": "L", "rank": 80, "country": "CZE",
        "hard":  {"svpt_won": 0.575, "rtpt_won": 0.378, "elo": 1955},
        "clay":  {"svpt_won": 0.558, "rtpt_won": 0.360, "elo": 1880},
        "grass": {"svpt_won": 0.590, "rtpt_won": 0.378, "elo": 2010},
    },
    "haddad_maia": {
        "full_name": "Beatriz Haddad Maia", "hand": "L", "rank": 24, "country": "BRA",
        "hard":  {"svpt_won": 0.552, "rtpt_won": 0.378, "elo": 1935},
        "clay":  {"svpt_won": 0.562, "rtpt_won": 0.392, "elo": 1985},
        "grass": {"svpt_won": 0.548, "rtpt_won": 0.368, "elo": 1900},
    },
    "kostyuk": {
        "full_name": "Marta Kostyuk", "hand": "R", "rank": 22, "country": "UKR",
        "hard":  {"svpt_won": 0.562, "rtpt_won": 0.385, "elo": 1975},
        "clay":  {"svpt_won": 0.552, "rtpt_won": 0.378, "elo": 1940},
        "grass": {"svpt_won": 0.558, "rtpt_won": 0.375, "elo": 1945},
    },
    "bencic": {
        "full_name": "Belinda Bencic", "hand": "R", "rank": 45, "country": "SUI",
        "hard":  {"svpt_won": 0.558, "rtpt_won": 0.385, "elo": 1965},
        "clay":  {"svpt_won": 0.548, "rtpt_won": 0.375, "elo": 1920},
        "grass": {"svpt_won": 0.555, "rtpt_won": 0.375, "elo": 1940},
    },
    "collins": {
        "full_name": "Danielle Collins", "hand": "R", "rank": 50, "country": "USA",
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
_SACKMANN_PROFILES: Dict[str, dict]  = {}  # player_key → rolling form/fatigue profile

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


def h2h_adj(p1: str, p2: str) -> float:
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
# FATIGUE & HOLD/BREAK MODELS (new in v2)
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
           {"svpt_won": 0.620, "rtpt_won": 0.340, "elo": 1800}))
    live_elo = _LIVE_ELO.get(key, {}).get(surf)
    if live_elo:
        base["elo"] = live_elo
    # Blend in Sackmann rolling stats (50/50) if available
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
# JEFF SACKMANN DATA — ROLLING FORM + FATIGUE (new in v2)
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
    cl     = csv_name.lower().strip()
    parts  = full_name.lower().split()
    if not parts or not cl:
        return False
    last = parts[-1]
    if last not in cl:
        return False
    # Require first-initial check for short/common surnames
    if len(last) < 6:
        first = parts[0][0] if parts[0] else ""
        csv_parts = cl.split()
        csv_first = csv_parts[0][0] if csv_parts and csv_parts[0] else ""
        return first == csv_first
    return True


def fetch_sackmann_matches(year: int = None) -> List[dict]:
    """Download ATP + WTA match CSVs from Jeff Sackmann's GitHub.
    Fetches current year; falls back to also include previous year if < 300 rows.
    """
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
        # Only go back a year if current year has too little data
        if y == year and year_rows >= 300:
            break
    return rows


def build_player_profile(all_matches: List[dict], full_name: str,
                         n: int = 20) -> Optional[dict]:
    """Compute rolling serve/return/form/fatigue profile from last n matches."""
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

    for row, is_winner in recent:
        prefix     = "w" if is_winner else "l"
        opp_prefix = "l" if is_winner else "w"

        sv = _calc_svpt_won(row, prefix)
        rt_opp = _calc_svpt_won(row, opp_prefix)
        if sv is not None:
            sv_wons.append(sv)
        if rt_opp is not None:
            rt_wons.append(1.0 - rt_opp)  # rtpt_won = 1 - opponent svpt_won

        results.append(1 if is_winner else 0)

        try:
            m = float(row.get("minutes") or 0)
            if m > 0:
                mins_list.append(m)
        except (ValueError, TypeError):
            pass

        score = row.get("score", "") or ""
        sets = len([s for s in score.split() if "-" in s])
        sets_list.append(max(1, sets))

    # Recency-weighted form rate
    weights = [1.0 / (i + 1.0) for i in range(len(results))]
    total_w = sum(weights)
    form_rate = sum(r * w for r, w in zip(results, weights)) / total_w if total_w > 0 else 0.5

    # Days since last match (for fatigue)
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

    return {
        "svpt_won":     round(sum(sv_wons) / len(sv_wons), 4) if sv_wons else None,
        "rtpt_won":     round(sum(rt_wons) / len(rt_wons), 4) if rt_wons else None,
        "form_rate":    round(form_rate, 4),
        "n_matches":    len(recent),
        "days_rest":    days_rest,
        "last_minutes": mins_list[0] if mins_list else 90.0,
        "last_sets":    sets_list[0] if sets_list else 3,
    }


def load_sackmann_data() -> None:
    """Fetch Sackmann CSVs and populate _SACKMANN_PROFILES + _RECENT_STATS."""
    all_matches = fetch_sackmann_matches()
    if not all_matches:
        log.warning("load_sackmann_data: no match data — using static stats only")
        return
    all_players = {**ATP_STATS, **WTA_STATS}
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
    log.info("load_sackmann_data: %d/%d players profiled", ok, len(all_players))


# ─────────────────────────────────────────────────────────────────────────────
# PREDICTION ENGINE  (v2 — 6-factor model)
# ─────────────────────────────────────────────────────────────────────────────

def predict(p1_key: str, p2_key: str, surface: str,
            tour_level: str = "atp250", best_of: int = 3) -> dict:
    """
    Base probability: 35% Surface ELO + 35% Markov Chain + 30% Hold/Break
    Adjustments:      fatigue ±8%  |  form ±5%  |  H2H ±5%
    """
    surf_adj = SURFACE_PT_ADJ.get(surface, 0.0)
    # get_surface_stats already blends static + _RECENT_STATS (from Sackmann)
    s1 = get_surface_stats(p1_key, surface)
    s2 = get_surface_stats(p2_key, surface)

    # Sackmann profiles supply fatigue & form; serve stats already in s1/s2
    prof1 = _SACKMANN_PROFILES.get(p1_key, {})
    prof2 = _SACKMANN_PROFILES.get(p2_key, {})

    # Effective serve win probability (Markov input)
    p1_sv = max(0.50, min(0.78, 0.5 * (s1["svpt_won"] + 1.0 - s2["rtpt_won"]) + surf_adj))
    p2_sv = max(0.50, min(0.78, 0.5 * (s2["svpt_won"] + 1.0 - s1["rtpt_won"]) + surf_adj))

    # === Model 1: Markov Chain (35%) ===
    markov_p1 = match_win_prob(p1_sv, p2_sv, best_of=best_of)

    # === Model 2: Surface ELO (35%) ===
    elo_p1 = elo_win_prob(s1.get("elo", 1800), s2.get("elo", 1800))

    # === Model 3: Hold / Break Dominance (30%) ===
    # hold = P(player wins serving game); break = P(player wins return game)
    hold1  = game_win_prob(max(0.50, min(0.80, s1["svpt_won"] + surf_adj)))
    hold2  = game_win_prob(max(0.50, min(0.80, s2["svpt_won"] + surf_adj)))
    break1 = game_win_prob(max(0.30, min(0.65, s1["rtpt_won"] - surf_adj)))
    break2 = game_win_prob(max(0.30, min(0.65, s2["rtpt_won"] - surf_adj)))
    hb_p1  = hold_break_win_prob(hold1, break1, hold2, break2)

    raw_prob = 0.35 * elo_p1 + 0.35 * markov_p1 + 0.30 * hb_p1

    # === Fatigue Adjustment (additive, ±8%) ===
    fat1 = fatigue_score(
        prof1.get("days_rest", 3),
        float(prof1.get("last_minutes", 90)),
        int(prof1.get("last_sets", 3)),
    )
    fat2 = fatigue_score(
        prof2.get("days_rest", 3),
        float(prof2.get("last_minutes", 90)),
        int(prof2.get("last_sets", 3)),
    )
    fat_adj_val = (fat2 - fat1) * 0.015  # positive → p1 fresher

    # === Form Adjustment (additive, ±5%) ===
    form1_rate = prof1.get("form_rate", 0.5)
    form2_rate = prof2.get("form_rate", 0.5)
    form_adj_val = (form1_rate - form2_rate) * 0.15

    # === H2H Adjustment (additive, ±5%) ===
    h2h_val = h2h_adj(p1_key, p2_key)

    blend = max(0.05, min(0.95,
                          raw_prob + fat_adj_val + form_adj_val + h2h_val))
    exp_g = expected_total_games(p1_sv, p2_sv, best_of=best_of)

    log.info(
        "predict %s vs %s [%s] markov=%.3f elo=%.3f hb=%.3f raw=%.3f "
        "fat=%+.3f form=%+.3f h2h=%+.3f → %.3f exp_g=%.1f",
        p1_key, p2_key, surface,
        markov_p1, elo_p1, hb_p1, raw_prob,
        fat_adj_val, form_adj_val, h2h_val, blend, exp_g,
    )

    return {
        "blend_p1":       round(blend, 4),
        "model_p1":       round(markov_p1, 4),
        "elo_p1":         round(elo_p1, 4),
        "hb_p1":          round(hb_p1, 4),
        "h2h_adj":        round(h2h_val, 4),
        "fat_adj":        round(fat_adj_val, 4),
        "form_adj":       round(form_adj_val, 4),
        "p1_sv":          round(p1_sv, 4),
        "p2_sv":          round(p2_sv, 4),
        "elo1":           s1.get("elo", 1800),
        "elo2":           s2.get("elo", 1800),
        "fatigue1":       round(fat1, 1),
        "fatigue2":       round(fat2, 1),
        "form1":          round(form1_rate, 3),
        "form2":          round(form2_rate, 3),
        "expected_games": round(exp_g, 1),
        "best_of":        best_of,
        "surface":        surface,
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
            "n_books":   len(hp),
            "sport":     game.get("_sport", ""),
            "commence":  game.get("commence_time", ""),
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
        reader = csv.DictReader(io.StringIO(r.text))
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


def generate_picks(matches: List[dict]) -> List[dict]:
    picks = []
    daily_exp = 0.0

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

        edge1 = blend_p1 - dv_p1
        edge2 = blend_p2 - dv_p2

        if edge1 >= edge2:
            edge, model_p, dv_p = edge1, blend_p1, dv_p1
            best_price, bet_name = odds_info["best_home"], odds_info["home"]
        else:
            edge, model_p, dv_p = edge2, blend_p2, dv_p2
            best_price, bet_name = odds_info["best_away"], odds_info["away"]

        if edge < MIN_EDGE_ML:
            continue
        if model_p < MIN_CONF_ML:
            continue
        if p1_key in _INJURIES or p2_key in _INJURIES:
            continue

        # 0.60→0.70  0.65→0.80  0.70→0.90  0.75+→1.0
        conf  = min(1.0, (model_p - MIN_CONF_ML) * 2.0 + 0.70)
        stake = kelly_stake(model_p, best_price, conf)
        stake = min(stake, MAX_DAILY_EXP - daily_exp)
        daily_exp += stake

        if edge >= 0.12:
            star = "💎"; tier = "A"
        elif edge >= 0.09:
            star = "⭐"; tier = "B"
        else:
            star = "•"; tier = "C"

        surface_emoji = {"clay": "🟤", "grass": "🟢", "hard": "🔵"}.get(m["surface"], "⚪")

        picks.append({
            "tier":          tier,
            "star":          star,
            "surface_emoji": surface_emoji,
            "tour":          TOUR_META.get(m["tour_level"], {}).get("name", m["tour_level"]),
            "tour_level":    m["tour_level"],
            "surface":       m["surface"],
            "p1":            odds_info["home"],
            "p2":            odds_info["away"],
            "p1_key":        p1_key,
            "p2_key":        p2_key,
            "bet_on":        bet_name,
            "best_price":    round(best_price, 3),
            "model_p":       round(model_p * 100, 1),
            "dv_p":          round(dv_p * 100, 1),
            "edge":          round(edge * 100, 1),
            "conf":          round(conf * 100, 1),
            "stake":         round(stake, 0),
            "p1_sv_pct":     round(pred["p1_sv"] * 100, 1),
            "p2_sv_pct":     round(pred["p2_sv"] * 100, 1),
            "elo1":          pred["elo1"],
            "elo2":          pred["elo2"],
            "expected_games": pred["expected_games"],
            "best_of":       pred["best_of"],
            "h2h_adj":       round(pred["h2h_adj"] * 100, 1),
            # v2 new fields
            "hb_p1":         round(pred.get("hb_p1", 0.5) * 100, 1),
            "form_adj":      round(pred.get("form_adj", 0.0) * 100, 1),
            "fat_adj":       round(pred.get("fat_adj", 0.0) * 100, 1),
            "fatigue1":      pred.get("fatigue1", 0.0),
            "fatigue2":      pred.get("fatigue2", 0.0),
            "form1":         round(pred.get("form1", 0.5) * 100, 1),
            "form2":         round(pred.get("form2", 0.5) * 100, 1),
            "commence":      odds_info.get("commence", ""),
        })
        log.info("  PICK %s %s vs %s → %s @%.2f model=%.1f%% edge=+%.1f%% $%.0f",
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
            json={"files": {"tennis_hist.json": {"content": json.dumps(hist, ensure_ascii=False, indent=2)}}},
            timeout=15,
        )
    except Exception as e:
        log.warning("save_history: %s", e)


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
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    lines = ["**🎾 ATP/WTA 每日預測 — %s**" % now.strftime("%Y-%m-%d %H:%M"), "```"]
    if not picks:
        lines.append("今日無符合條件的推薦")
    else:
        for p in picks:
            lines.append("%s %s %s vs %s" % (p["star"], p["surface_emoji"], p["p1"], p["p2"]))
            lines.append("  推薦: %s @%.2f  模型:%.1f%%  edge:+%.1f%%  $%.0f" % (
                p["bet_on"], p["best_price"], p["model_p"], p["edge"], p["stake"]))
            if p.get("fat_adj", 0) or p.get("form_adj", 0):
                lines.append("  體能:%+.1f%%  狀態:%+.1f%%  H/B:%.1f%%" % (
                    p.get("fat_adj", 0), p.get("form_adj", 0), p.get("hb_p1", 50)))
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
        "generated_at": now.strftime("%Y-%m-%d %H:%M") + " (台灣時間)",
        "date":         now.strftime("%Y-%m-%d"),
        "model_version": "v2.0 — 6-factor",
        "stats":        stats,
        "picks":        picks,
        "recent_history": list(reversed(history.get("bets", [])[-10:])),
        "live_matches": [],
        "game_preds":   game_preds,
    }
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("Wrote %s (%d picks)", JSON_PATH, len(picks))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    now_tw = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    log.info("=== Tennis Bot v2.0 start %s ===", now_tw.strftime("%Y-%m-%d %H:%M"))

    fetch_ta_elo()       # static ELO override (no-op if CSV lacks elo column)
    load_sackmann_data() # rolling form/fatigue from ATP+WTA match CSVs

    raw_odds = fetch_odds()
    odds_map = parse_odds(raw_odds)

    matches: List[dict] = []
    game_preds: Dict[str, dict] = {}

    for key, odds_info in odds_map.items():
        sport   = odds_info.get("sport", "tennis_atp")
        surface = infer_surface(sport)
        t_lvl   = infer_tour_level(sport)
        best_of = TOUR_META.get(t_lvl, {}).get("best_of", 3)

        p1_key = norm_player(odds_info["home"])
        p2_key = norm_player(odds_info["away"])

        pred = predict(p1_key, p2_key, surface, t_lvl, best_of)

        game_preds[key] = {
            "p1": odds_info["home"], "p2": odds_info["away"],
            "p1_key": p1_key, "p2_key": p2_key,
            "model_p1": pred["blend_p1"],
            "surface": surface, "tour_level": t_lvl,
            "best_of": best_of, "exp_games": pred["expected_games"],
        }
        matches.append({
            "p1_key": p1_key, "p2_key": p2_key,
            "surface": surface, "tour_level": t_lvl, "best_of": best_of,
            "odds_info": odds_info, "pred": pred,
        })

    log.info("Processed %d matches", len(matches))

    picks   = generate_picks(matches)
    history = load_history()
    stats   = compute_stats(history)

    write_json(picks, stats, history, game_preds, now_tw)

    if picks:
        send_ntfy(
            "🎾 Tennis Picks — %s" % now_tw.strftime("%m/%d"),
            "%d 個推薦\n" % len(picks) +
            "\n".join("• %s vs %s → %s @%.2f (+%.1f%%)" % (
                p["p1"], p["p2"], p["bet_on"], p["best_price"], p["edge"]
            ) for p in picks),
        )
    send_discord(picks, stats)
    log.info("=== Done — %d picks ===", len(picks))


if __name__ == "__main__":
    run()
