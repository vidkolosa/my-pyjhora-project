# server/app.py — v0.9.1 (Whole-Sign only; endpoints: /chart_full, /chart_place, /chart_full_place)
import os, sys, re
from typing import Optional, Dict
from datetime import datetime

from fastapi import FastAPI, HTTPException
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

# ---------- Swiss Ephemeris setup ----------
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)            # just in case
swe.set_ephe_path(EPHE_PATH)

# Lahiri sidereal; Mean node — JHora-style
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS_SID = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

APP_VERSION = "0.9.1"

# ---------- Schemas ----------
class BirthData(BaseModel):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    lat: float
    lon: float
    tz: Optional[float] = Field(default=None, description="Hours from UTC; if null, auto by coords")

class PlaceTime(BaseModel):
    place: str
    datetime_local: str  # "YYYY-MM-DD HH:MM"

# ---------- Utils ----------
def norm(x: float) -> float:
    x %= 360.0
    return x if x >= 0 else x + 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def tz_offset_hours(lat: float, lon: float, dt: datetime, tz_override: Optional[float]) -> float:
    """
    Vrne zamik v urah od UTC. Če je podan tz_override, ga uporabi.
    JHora-kompatibilni popravek: za Evropo pred 1970 upoštevamo fiksne standardne offsete (brez DST).
    """
    if tz_override is not None:
        return float(tz_override)

    tf = TimezoneFinder()
    tzname = tf.timezone_at(lat=lat, lng=lon) or "UTC"

    # --- JHora-compatibility patch for historical Europe ---
    # JHora za 1960-ta leta v Jugoslaviji računa standardni čas (brez poletnega).
    if tzname.startswith("Europe/") and dt.year < 1970:
        # Groba delitev po geografski dolžini na WET(0), CET(+1), EET(+2)
        # WET:   lon < 7.5°E  → 0
        # CET:   7.5°E–22.5°E → +1  (Slovenija ~16°E → +1)
        # EET:   ≥22.5°E      → +2
        if lon < 7.5:
            return 0.0
        elif lon < 22.5:
            return 1.0
        else:
            return 2.0

    # Sicer uporabi IANA/pytz (moderna pravila z DST)
    tzinfo = pytz.timezone(tzname)
    local = tzinfo.localize(dt)
    return local.utcoffset().total_seconds() / 3600.0


def julday_ut_from_local(bd: BirthData) -> float:
    dt_local = datetime(bd.year, bd.month, bd.day, bd.hour, bd.minute)
    ut = bd.hour + bd.minute / 60.0 - tz_offset_hours(bd.lat, bd.lon, dt_local, bd.tz)
    return swe.julday(bd.year, bd.month, bd.day, ut)

def ascendant_sid(jd_ut: float, lat: float, lon: float) -> float:
    # 'P' = Placidus in swe.houses_ex, vendar vzamemo samo ASC; hiše računamo kot Whole-Sign.
    asc = swe.houses_ex(jd_ut, lat, lon, b'P', FLAGS_SID)[1][0]
    return norm(asc)

def planets_sid(jd_ut: float) -> Dict[str, float]:
    ids = {
        "Sun": swe.SUN, "Moon": swe.MOON, "Mars": swe.MARS, "Mercury": swe.MERCURY,
        "Jupiter": swe.JUPITER, "Venus": swe.VENUS, "Saturn": swe.SATURN, "Rahu": swe.MEAN_NODE
    }
    out = {k: norm(swe.calc_ut(jd_ut, pid, FLAGS_SID)[0][0]) for k, pid in ids.items()}
    out["Ketu"] = norm(out["Rahu"] + 180.0)
    return out

def bhava_whole(lon: float, lagna_sign_idx: int) -> int:
    s = int(lon // 30)              # sign index 0..11
    d = (s - lagna_sign_idx) % 12
    return d + 1

def chara_karakas(pl):
    use = ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]
    # Rahu “degree within sign” šteje obratno
    within = {p: (30.0 - (pl[p] % 30.0) if p == "Rahu" else (pl[p] % 30.0)) for p in use}
    order = ["Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
             "Pitrukaraka","Putrakaraka","Gnatikaraka","Darakaraka"]
    ranked = sorted(use, key=lambda x: (within[x], pl[x] % 30.0), reverse=True)
    return {order[i]: {"planet": ranked[i], "sign": sign_of(pl[ranked[i]])} for i in range(8)}

def parse_place_to_latlon(txt: str) -> Optional[tuple]:
    """
    Sprejme:
      - DMS z N/S/E/W (katerikoli vrstni red): "21 N 27' 00, 83 E 58' 00"
      - Decimal + N/S/E/W: "21.27 N, 83.97 E"
      - Čisti decimalni "lat, lon": "21.27, 83.97" (S/W negativna)
      - Mesto (vrne None; potem uporabimo geocoder ali fallback)
    """
    s = txt.replace("°", "'").replace("’", "'").strip()

    # DMS z oznakami N/S/E/W
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

# ---------- FastAPI app ----------
app = FastAPI(title="JHora PyAPI", version=APP_VERSION)

@app.get("/health")
def health():
    ephe_ok = os.path.isdir(EPHE_PATH) and bool(os.listdir(EPHE_PATH))
    return {"ok": True, "version": APP_VERSION, "ephe_loaded": ephe_ok}

# Warm-up mora biti definirán PO tem, ko imamo app!
def _warm_once():
    try:
        bd = BirthData(year=2000, month=1, day=1, hour=0, minute=0, lat=0.0, lon=0.0, tz=0)
        _ = chart_full(bd)   # samo sproži nalaganje; rezultat ignoriramo
    except Exception:
        # če efemeride še niso na mestu, health jih bo pokazal z ephe_loaded=False
        pass

@app.on_event("startup")
def _startup_warm():
    _warm_once()

# ---------- Endpoints ----------
@app.post("/chart_full")
def chart_full(bd: BirthData):
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
        "house_system": "Whole-Sign (Rāśi)",
        "ascendant": {"deg": round(asc, 2), "sign": sign_of(asc)},
        "house_cusps_whole": {f"House{i+1}": round(cusps_whole[i], 2) for i in range(12)},
        "planets": planets,
        "chara_karakas": chara_karakas(pl)
    }

@app.post("/chart_place")
def chart_place(req: PlaceTime):
    coords = parse_place_to_latlon(req.place)
    if coords is None:
        # mini fallback za demo: Maribor
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

@app.post("/chart_full_place")
def chart_full_place(req: PlaceTime):
    return chart_place(req)
