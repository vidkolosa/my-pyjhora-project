# server/app.py — v0.9.3
# Whole-Sign (Rāśi) hiše, Lahiri ayanamsa, Mean node (JHora style)
# EU pre-1970 TZ patch (brez DST) + ayanamsa micro-correction prek ENV.

import os, sys, re
from typing import Optional, Dict, Tuple
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import swisseph as swe
import pytz
from timezonefinder import TimezoneFinder

# -----------------------------------------------------------------------------
# Paths (add ../src to PYTHONPATH → ephemeris je v src/jhora/data/ephe)
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# -----------------------------------------------------------------------------
# Swiss Ephemeris
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# Lahiri sidereal; Mean node — JHora-style
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS_SID = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL

# -----------------------------------------------------------------------------
APP_VERSION = "0.9.3"

# Majhen popravek ayanamše (deg), nastavljen prek ENV.
# Primer: AYANAMSHA_CORR_DEG=0.8867 (približek za 1960s JHora match)
AYAN_CORR = float(os.getenv("AYANAMSHA_CORR_DEG", "0"))

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

# -----------------------------------------------------------------------------
# Schemas
class BirthData(BaseModel):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    lat: float
    lon: float
    tz: Optional[float] = Field(default=None, description="UTC offset in hours; if null → auto by coords")

class PlaceTime(BaseModel):
    place: str
    datetime_local: str  # "YYYY-MM-DD HH:MM"

# -----------------------------------------------------------------------------
# Utilities
def norm(x: float) -> float:
    x %= 360.0
    return x if x >= 0 else x + 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def parse_place_to_latlon(txt: str) -> Optional[Tuple[float, float]]:
    """
    Sprejme:
      - DMS: "21 N 27' 00, 83 E 58' 00" (poljuben vrstni red)
      - Decimal + hemisfere: "21.27 N, 83.97 E"
      - Čisti decimal: "21.27, 83.97"  (lat, lon; S/W negativno)
      - Mesto: "Maribor, Slovenia" (vrne None → naj to obdela koda višje)
    """
    s = txt.replace("°", "'").replace("’", "'").strip()

    # DMS s hemisferami
    dms_pat = r"(\d+)(?:'|\s)?\s*(\d+)?(?:'|\s)?\s*(\d+)?\s*([NnSsEeWw])"
    tokens = re.findall(dms_pat, s)
    lat = lon = None
    if tokens:
        for deg, minu, sec, hemi in tokens:
            v = float(deg) + (float(minu or 0)/60.0) + (float(sec or 0)/3600.0)
            hemi = hemi.upper()
            if hemi in ("N","S"):
                lat = v if hemi == "N" else -v
            elif hemi in ("E","W"):
                lon = v if hemi == "E" else -v
        if lat is not None and lon is not None:
            return (lat, lon)

    # Decimal + hemisfere
    dec_hemi = re.findall(r"(-?\d+(?:\.\d+)?)\s*([NS])|(-?\d+(?:\.\d+)?)\s*([EW])", s, re.I)
    if dec_hemi:
        for a, ns, c, ew in dec_hemi:
            if ns:
                lat = float(a) * (1 if ns.upper()=="N" else -1)
            if ew:
                lon = float(c) * (1 if ew.upper()=="E" else -1)
        if lat is not None and lon is not None:
            return (lat, lon)

    # Čisti decimal "lat, lon"
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))

    return None

def tz_offset_hours(lat: float, lon: float, dt: datetime, tz_override: Optional[float]) -> float:
    """
    Vrne zamik v urah od UTC.
    - če je podan tz_override → uporabi njega
    - sicer uporablja IANA/pytz (moderna DST pravila)
    - PATCH: za Evropo pred 1970 uporabi standardni čas (brez DST), JHora-style.
    """
    if tz_override is not None:
        return float(tz_override)

    tf = TimezoneFinder()
    tzname = tf.timezone_at(lat=lat, lng=lon) or "UTC"

    # JHora kompatibilnost za stare evropske datume (npr. Jugoslavija 1960s)
    if tzname.startswith("Europe/") and dt.year < 1970:
        # Grob razrez (WET/CET/EET) zadošča za ujemanje JHora:
        if lon < 7.5:
            return 0.0      # WET
        elif lon < 22.5:
            return 1.0      # CET → Slovenija
        else:
            return 2.0      # EET

    tzinfo = pytz.timezone(tzname)
    local = tzinfo.localize(dt)
    return local.utcoffset().total_seconds() / 3600.0

def julday_ut_from_local(bd: BirthData) -> float:
    dt_local = datetime(bd.year, bd.month, bd.day, bd.hour, bd.minute)
    ut = bd.hour + bd.minute/60.0 - tz_offset_hours(bd.lat, bd.lon, dt_local, bd.tz)
    return swe.julday(bd.year, bd.month, bd.day, ut)

def ascendant_sid(jd_ut: float, lat: float, lon: float) -> float:
    asc = swe.houses_ex(jd_ut, lat, lon, b'P', FLAGS_SID)[1][0]
    return norm(asc + AYAN_CORR)

def planets_sid(jd_ut: float) -> Dict[str, float]:
    ids = {
        "Sun": swe.SUN, "Moon": swe.MOON, "Mars": swe.MARS, "Mercury": swe.MERCURY,
        "Jupiter": swe.JUPITER, "Venus": swe.VENUS, "Saturn": swe.SATURN, "Rahu": swe.MEAN_NODE
    }
    out: Dict[str, float] = {}
    for name, pid in ids.items():
        lon = swe.calc_ut(jd_ut, pid, FLAGS_SID)[0][0]
        out[name] = norm(lon + AYAN_CORR)
    out["Ketu"] = norm(out["Rahu"] + 180.0)
    return out

def bhava_whole(lon: float, lagna_sign_idx: int) -> int:
    s = int(lon // 30)           # sign index 0..11
    d = (s - lagna_sign_idx) % 12
    return d + 1                 # 1..12

def chara_karakas(pl: Dict[str, float]) -> Dict[str, Dict[str, str]]:
    use = ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]
    within = {
        p: (30.0 - (pl[p] % 30.0) if p == "Rahu" else (pl[p] % 30.0))
        for p in use
    }
    order = [
        "Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
        "Pitrukaraka","Putrakaraka","Gnatikaraka","Darakaraka"
    ]
    ranked = sorted(use, key=lambda x: (within[x], pl[x] % 30.0), reverse=True)
    return {order[i]: {"planet": ranked[i], "sign": sign_of(pl[ranked[i]])} for i in range(8)}

# -----------------------------------------------------------------------------
# FastAPI
app = FastAPI(title="JHora PyAPI", version=APP_VERSION)

@app.get("/health")
def health():
    ephe_ok = os.path.isdir(EPHE_PATH) and bool(os.listdir(EPHE_PATH))
    return {"ok": True, "version": APP_VERSION, "ephe_loaded": ephe_ok, "ayan_corr_deg": AYAN_CORR}

# Warm-up (preload ephemeris + 1 dummy izračun)
@app.on_event("startup")
def _startup_warm():
    try:
        bd = BirthData(year=2000, month=1, day=1, hour=0, minute=0, lat=0.0, lon=0.0, tz=0)
        _ = chart_full(bd)  # ignore result
    except Exception:
        pass

# -----------------------------------------------------------------------------
# 1) DIRECT: numeric lat/lon
@app.post("/chart_full")
def chart_full(bd: BirthData):
    jd = julday_ut_from_local(bd)
    pl = planets_sid(jd)
    asc = ascendant_sid(jd, bd.lat, bd.lon)

    lagna_sign_idx = int(asc // 30)
    cusps_whole = [((lagna_sign_idx + i) % 12) * 30.0 for i in range(12)]

    planets = {
        p: {
            "deg": round(lon, 2),
            "sign": sign_of(lon),
            "bhava": bhava_whole(lon, lagna_sign_idx)
        }
        for p, lon in pl.items()
    }

    return {
        "ayanamsa": "Lahiri",
        "ayan_corr_deg": AYAN_CORR,
        "node": "Mean",
        "house_system": "Whole-Sign (Rāśi)",
        "ascendant": {"deg": round(asc, 2), "sign": sign_of(asc)},
        "house_cusps_whole": {f"House{i+1}": round(cusps_whole[i], 2) for i in range(12)},
        "planets": planets,
        "chara_karakas": chara_karakas(pl)
    }

# 2) PLACE/COORDS string
@app.post("/chart_place")
def chart_place(req: PlaceTime):
    coords = parse_place_to_latlon(req.place)
    if coords is None:
        # Mini fallback: "Maribor" kot primer
        if "maribor" in req.place.lower():
            coords = (46.55, 15.98)
        else:
            raise HTTPException(
                400,
                "Unsupported 'place'. Use 'City, Country' or coordinates like \"21 N 27' 00, 83 E 58' 00\" or \"21.27, 83.97\"."
            )

    lat, lon = coords
    try:
        date_part, time_part = req.datetime_local.strip().split(" ")
        y, m, d = map(int, date_part.split("-"))
        hh, mm = map(int, time_part.split(":"))
    except Exception:
        raise HTTPException(400, "Invalid datetime_local format. Use 'YYYY-MM-DD HH:MM'.")

    bd = BirthData(year=y, month=m, day=d, hour=hh, minute=mm, lat=lat, lon=lon, tz=None)
    return chart_full(bd)

# 3) BRIDGE (isti kot chart_place; ime je bolj jasno za GPT)
@app.post("/chart_full_place")
def chart_full_place(req: PlaceTime):
    return chart_place(req)
