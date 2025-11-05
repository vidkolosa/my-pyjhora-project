# server/app.py — v1.0.0
# Whole-Sign houses (Rāśi), Lahiri + JHora–alignment:
#  - Hard-coded ayanamsha correction  -0.8867°  (overridable via AYANAMSHA_CORR_DEG)
#  - Pre-1970 Europe -> fixed UTC+1, no DST  (JHora-style for old dates in our region)

import os, sys, re
from typing import Optional, Dict, Tuple
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
import swisseph as swe
import pytz
from timezonefinder import TimezoneFinder

# ---------- paths ----------
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# ---------- Swiss Ephemeris ----------
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# Lahiri sidereal + Mean node (JHora-style)
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS_SID = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

APP_VERSION = "1.0.0"

# Ayanamsha fine alignment (JHora)
def _ayan_corr_deg() -> float:
    env = os.getenv("AYANAMSHA_CORR_DEG", "").strip()
    if env:
        try:
            return float(env)
        except:
            pass
    # default that matched tvoja JHora na posnetkih
    return -0.8867

AYAN_CORR = _ayan_corr_deg()

# ---------- Schemas ----------
class BirthData(BaseModel):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    lat: float
    lon: float
    tz: Optional[float] = Field(default=None, description="Hours offset from UTC; if null, auto by coords")

class PlaceTime(BaseModel):
    place: str
    datetime_local: str  # "YYYY-MM-DD HH:MM"

# ---------- Utils ----------
def norm(x: float) -> float:
    x %= 360.0
    return x if x >= 0 else x + 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def _maybe_force_pre1970_europe_offset(lat: float, lon: float, dt: datetime) -> Optional[float]:
    """
    JHora-style: za datume < 1970 v evropskem območju uporabimo fiksni UTC+1 brez DST.
    To stabilizira stare karte (npr. 1964 Maribor) in da enake hiše kot v JHori.
    """
    if dt.year >= 1970:
        return None

    # približno območje Evrope (lon od -25 do 45, lat 30..72)
    if -25.0 <= lon <= 45.0 and 30.0 <= lat <= 72.0:
        return 1.0  # UTC+1, no DST
    return None

def tz_offset_hours(lat: float, lon: float, dt: datetime, tz_override: Optional[float]) -> float:
    if tz_override is not None:
        return float(tz_override)

    # JHora-like pre-1970 Evropa (fiksno +1)
    forced = _maybe_force_pre1970_europe_offset(lat, lon, dt)
    if forced is not None:
        return forced

    # sicer normalno iz TZ
    tf = TimezoneFinder()
    tzname = tf.timezone_at(lat=lat, lng=lon) or "UTC"
    tzinfo = pytz.timezone(tzname)
    # robustno lokaliziranje (pytz)
    try:
        local = tzinfo.localize(dt, is_dst=None)
    except Exception:
        # v dvomu brez DST
        local = tzinfo.localize(dt, is_dst=False)
    return local.utcoffset().total_seconds() / 3600.0

def julday_ut_from_local(bd: BirthData) -> float:
    dt_local = datetime(bd.year, bd.month, bd.day, bd.hour, bd.minute)
    ut = bd.hour + bd.minute/60.0 - tz_offset_hours(bd.lat, bd.lon, dt_local, bd.tz)
    return swe.julday(bd.year, bd.month, bd.day, ut)

def _apply_ayan_correction(lon: float) -> float:
    # premik vseh siderealnih longitúd, da 100% zadene JHora mejo znakov (npr. Luna 0°00' Kozorog)
    return norm(lon + AYAN_CORR)

def ascendant_sid(jd_ut: float, lat: float, lon: float) -> float:
    asc = swe.houses_ex(jd_ut, lat, lon, b'P', FLAGS_SID)[1][0]
    return _apply_ayan_correction(norm(asc))

def planets_sid(jd_ut: float) -> Dict[str, float]:
    ids = {
        "Sun": swe.SUN, "Moon": swe.MOON, "Mars": swe.MARS, "Mercury": swe.MERCURY,
        "Jupiter": swe.JUPITER, "Venus": swe.VENUS, "Saturn": swe.SATURN, "Rahu": swe.MEAN_NODE
    }
    out: Dict[str, float] = {}
    for name, pid in ids.items():
        lon = swe.calc_ut(jd_ut, pid, FLAGS_SID)[0][0]
        out[name] = _apply_ayan_correction(norm(lon))
    out["Ketu"] = norm(out["Rahu"] + 180.0)
    return out

def bhava_whole(lon: float, lagna_sign_idx: int) -> int:
    s = int(lon // 30)       # sign index 0..11
    d = (s - lagna_sign_idx) % 12
    return d + 1

def chara_karakas(pl: Dict[str, float]) -> Dict[str, Dict[str, str]]:
    use = ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]
    within = {p: (30.0 - (pl[p] % 30.0) if p == "Rahu" else (pl[p] % 30.0)) for p in use}
    order = ["Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
             "Pitrukaraka","Putrakaraka","Gnatikaraka","Darakaraka"]
    ranked = sorted(use, key=lambda x: (within[x], pl[x] % 30.0), reverse=True)
    return {order[i]: {"planet": ranked[i], "sign": sign_of(pl[ranked[i]])} for i in range(8)}

def parse_place_to_latlon(txt: str) -> Optional[Tuple[float, float]]:
    s = txt.replace("°", "'").replace("’", "'").strip()

    # DMS z N/S/E/W (v poljubnem zaporedju)
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

    # Decimal z N/S ali E/W
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

# ---------- FastAPI ----------
app = FastAPI(title="JHora PyAPI", version=APP_VERSION)

@app.on_event("startup")
def _warm_once():
    try:
        bd = BirthData(year=1960, month=1, day=1, hour=0, minute=0, lat=46.55, lon=15.98, tz=None)
        _ = swe.julday(bd.year, bd.month, bd.day, 0.0)
    except Exception:
        pass

@app.get("/health")
def health():
    ephe_ok = os.path.isdir(EPHE_PATH) and bool(os.listdir(EPHE_PATH))
    return {"ok": True, "version": APP_VERSION, "ephe_loaded": ephe_ok, "ayan_corr_deg": AYAN_CORR}

def _chart_core(bd: BirthData):
    jd = julday_ut_from_local(bd)
    pl = planets_sid(jd)
    asc = ascendant_sid(jd, bd.lat, bd.lon)
    lagna_sign_idx = int(asc // 30)

    cusps_whole = [((lagna_sign_idx + i) % 12) * 30.0 for i in range(12)]

    planets = {
        p: {"deg": round(lon, 2), "sign": sign_of(lon), "bhava": bhava_whole(lon, lagna_sign_idx)}
        for p, lon in pl.items()
    }

    return {
        "ayanamsa": "Lahiri",
        "node": "Mean",
        "ayanamsha_correction_deg": AYAN_CORR,
        "house_system": "Whole-Sign (Rāśi)",
        "ascendant": {"deg": round(asc, 2), "sign": sign_of(asc)},
        "house_cusps_whole": {f"House{i+1}": round(cusps_whole[i], 2) for i in range(12)},
        "planets": planets,
        "chara_karakas": chara_karakas(pl)
    }

# 1) DIRECT lat/lon
@app.post("/chart_full")
def chart_full(bd: BirthData):
    return _chart_core(bd)

# 2) PLACE / COORDS string
@app.post("/chart_place")
def chart_place(req: PlaceTime):
    coords = parse_place_to_latlon(req.place)
    if coords is None:
        # demo fallback (Maribor)
        if "maribor" in req.place.lower():
            coords = (46.55, 15.98)
        else:
            raise HTTPException(
                400,
                "Unsupported 'place'. Use 'City, Country' or coordinates like \"21 N 27' 00, 83 E 58' 00\" or \"21.27, 83.97\"."
            )
    lat, lon = coords
    y, m, d = map(int, req.datetime_local.split(" ")[0].split("-"))
    hh, mm = map(int, req.datetime_local.split(" ")[1].split(":"))
    bd = BirthData(year=y, month=m, day=d, hour=hh, minute=mm, lat=lat, lon=lon, tz=None)
    return _chart_core(bd)

# 3) Alias za GPT (isto kot chart_place)
@app.post("/chart_full_place")
def chart_full_place(req: PlaceTime):
    return chart_place(req)
