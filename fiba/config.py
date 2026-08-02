"""Shared paths and event configuration."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "docs"   # GitHub Pages serves main branch /docs
TEMPLATES_DIR = ROOT / "templates"
DB_PATH = DATA_DIR / "fiba.db"

BASE_URL = "https://www.fiba.basketball"

# The event we are actually covering.
EVENT_SLUG = "fiba-u16-eurobasket-2026-division-b"

# Events used to build and validate the pipeline before tip-off.
TEST_EVENTS = [
    "fiba-u16-eurobasket-2025-division-b",  # 81 finished games, same age group
    "fiba-u18-eurobasket-2026-division-b",  # Ireland played 7 games here
]

# FIBA organisation id for Ireland. Stable across events.
IRELAND_ORG_ID = 81

# FIBA youth games are 4 x 10 minutes.
PERIOD_SECONDS = 600
REGULATION_PERIODS = 4
OT_SECONDS = 300
TEAM_MINUTES_PER_GAME = 200  # 5 players x 40 minutes

# Shot coordinate calibration, derived empirically from the embedded feed.
# Both teams' shots are normalised to a single half court.
BASKET_X = 140.0
BASKET_Y = 28.0
UNITS_PER_METRE = 17.9
THREE_POINT_RADIUS_M = 6.75  # FIBA U16 arc

for _d in (RAW_DIR, DATA_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
