#!/usr/bin/env python3
"""
Tennis Bot v3.2 — ATP/WTA 巡迴賽預測系統
9因子模型：Surface ELO 25% + Markov Chain 25% + Hold/Break 20% + Adjusted for surface/h2h
"""

import csv
import datetime
import io
import json
import logging
import math
import os
import re
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("tennis_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ─────────────────────────────────────────────────────────────────────────────
# ENV / CONFIG
# ─────────────────────────────────────────────────────────────────────────────
ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "")
GIST_TOKEN     = os.environ.get("GIST_TOKEN", "")
GIST_ID        = os.environ.get("GIST_ID", "")
NTFY_TOPIC     = os.environ.get("NTFY_TOPIC", "tennis-picks")
MODE           = os.environ.get("MODE", "full")   # full | live

MIN_EDGE       = 4.0    # minimum edge % to include a pick
MIN_BOOKS      = 2      # minimum bookmakers for odds validity
KELLY_FRAC     = 0.25   # fractional Kelly
BANKROLL       = 1000
