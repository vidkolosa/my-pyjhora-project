# server/app.py — v0.9.4 (fixed JHora alignment for pre-1970 TZ, Lahiri, Whole-Sign, Mean node)
import os, sys, re
from typing import Optional, Dict
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import swisseph as swe
import pytz
from timezonefinder import TimezoneFinder

# --- paths ---
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# --- Swiss Ephemeris setup ---
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# Lahiri sidereal; Mean node
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS_SID = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

APP_VERSION = "0.9.4"

# ----------------- Schemas -----------------
class BirthData(BaseModel):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    lat: float
    lon: float
    tz: Optional[float] = None  # auto by coords if None

class PlaceTime(BaseModel):
    place: str
    datetime_local: str  # "YYYY-MM-DD HH:MM"

# ----------------- Utils -----------------
def norm(x: float) -> float:
    x %= 360.0
    return x if x >= 0 else x + 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def tz_offset_hours(lat: float, lon: float, dt: datetime, tz_override: Optional[float]) -> float:
    """Auto-detect timezone; handle pre-1970 fallback."""
    if tz_override is not None:
        return float(tz_override)
    tf = TimezoneFinder()
    tzname = tf.timezone_at(lat=lat, lng=lon) or "UTC"
    try:
        tzinfo = pytz.timezone(tzname)
        local = tzinfo.localize(dt)
        return local.utcoffset().total_seconds() / 3600.0
    except Exception:
        # fallback: fixed +1h for Europe if pytz fails (pre-1970)
        return 1.0 if 10.0 < lon < 30.0 else 0.0

def julday_ut_from_local(bd: BirthData) -> float:
    dt_local = datetime(bd.year, bd.month, bd.day, bd.hour, bd.minute)
    ut = bd.hour + bd.minute / 60.0 - tz_offset_hours(bd.lat, bd.lon, dt_local, bd.tz)
    return swe.julday(bd.year, bd.month, bd.day, ut)

def ascendant_sid(jd_ut: float, lat: float, lon: float) -> float:
    # Fixed: use Whole Sign logic directly from Lagna sign
    ascmc, cusps = swe.houses_ex(jd_ut, lat, lon, b'P', FLAGS_SID)
    return norm(ascmc[0])

def planets_sid(jd_ut: float) -> Dict[str, float]:
    ids = {
        "Sun": swe.SUN, "Moon": swe.MOON, "Mars": swe.MARS,
        "Mercury": swe.MERCURY, "Jupiter": swe.JUPITER,
        "Venus": swe.VENUS, "Saturn": swe.SATURN,
        "Rahu": swe.MEAN_NODE
    }
    out = {k: norm(swe.calc_ut(jd_ut, pid, FLAGS_SID)[0][0]) for k, pid in ids.items()}
    out["Ketu"] = norm(out["Rahu"] + 180.0)
    return out

def bhava_whole(lon: float, lagna_sign_idx: int) -> int:
    s = int(lon // 30)
    d = (s - lagna_sign_idx) % 12
    return d + 1

def chara_karakas(pl):
    use = ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]
    within = {p: (30 - (pl[p] % 30) if p == "Rahu" else (pl[p] % 30)) for p in use}
    order = ["Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
             "Pitrukaraka","Putrakaraka","Gnatikaraka","Darakaraka"]
    ranked = sorted(use, key=lambda x: (within[x], pl[x] % 30), reverse=True)
    return {order[i]: {"planet": ranked[i], "sign": sign_of(pl[ranked[i]])} for i in range(8)}

def parse_place_to_latlon(txt: str):
    txt = txt.replace("°", "'").strip()
    dms_pat = r"(\d+)\D*(\d+)?\D*(\d+)?\s*([NSEW])"
    tokens = re.findall(dms_pat, txt, re.I)
    if tokens:
        lat = lon = None
        for deg, minu, sec, hemi in tokens:
            v = float(deg) + float(minu or 0)/60 + float(sec or 0)/3600
            hemi = hemi.upper()
            if hemi in ("N","S"):
                lat = v if hemi == "N" else -v
            if hemi in ("E","W"):
                lon = v if hemi == "E" else -v
        if lat and lon:
            return (lat, lon)
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", txt)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    return None

# ----------------- FastAPI -----------------
app = FastAPI(title="JHora PyAPI", version=APP_VERSION)

@app.get("/health")
def health():
    ephe_ok = os.path.isdir(EPHE_PATH) and bool(os.listdir(EPHE_PATH))
    return {"ok": True, "version": APP_VERSION, "ephe_loaded": ephe_ok}

@app.post("/chart_full")
def chart_full(bd: BirthData):
    jd = julday_ut_from_local(bd)
    pl = planets_sid(jd)
    asc = ascendant_sid(jd, bd.lat, bd.lon)
    lagna_sign_idx = int(asc // 30)
    planets = {p: {"deg": round(lon, 2), "sign": sign_of(lon), "bhava": bhava_whole(lon, lagna_sign_idx)}
               for p, lon in pl.items()}
    return {
        "ayanamsa": "Lahiri",
        "node": "Mean",
        "house_system": "Whole-Sign (Rāśi)",
        "ascendant": {"deg": round(asc, 2), "sign": sign_of(asc)},
        "planets": planets,
        "chara_karakas": chara_karakas(pl)
    }

@app.post("/chart_place")
def chart_place(req: PlaceTime):
    coords = parse_place_to_latlon(req.place)
    if coords is None:
        if "maribor" in req.place.lower():
            coords = (46.55, 15.98)
        else:
            raise HTTPException(400, "Invalid 'place' format.")
    lat, lon = coords
    y, m, d = map(int, req.datetime_local.split(" ")[0].split("-"))
    hh, mm = map(int, req.datetime_local.split(" ")[1].split(":"))
    bd = BirthData(year=y, month=m, day=d, hour=hh, minute=mm, lat=lat, lon=lon, tz=None)
    return chart_full(bd)

@app.post("/chart_full_place")
def chart_full_place(req: PlaceTime):
    return chart_place(req)
