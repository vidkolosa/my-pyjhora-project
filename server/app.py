# server/app.py — v0.7.0 (JHora-accurate Sripati; correct wrap-around; no D9)
import os, sys
from typing import Optional, Dict
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import swisseph as swe
import pytz
from timezonefinder import TimezoneFinder

# --- paths ---
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# --- Swiss Ephemeris ---
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)
# JHora style: sidereal Lahiri, mean node
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS_SID = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

APP_VERSION = "0.7.0"

class BirthData(BaseModel):
    year: int; month: int; day: int
    hour: int; minute: int
    lat: float; lon: float
    tz: Optional[float] = None  # auto if None

app = FastAPI(title="JHora PyAPI", version=APP_VERSION)

# --- helpers ---
def norm(x: float) -> float:
    x %= 360.0
    return x if x >= 0 else x + 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def tz_offset(bd: BirthData) -> float:
    if bd.tz is not None:
        return float(bd.tz)
    tf = TimezoneFinder()
    tzname = tf.timezone_at(lat=bd.lat, lng=bd.lon) or "Europe/Ljubljana"
    tzinfo = pytz.timezone(tzname)
    local = tzinfo.localize(datetime(bd.year, bd.month, bd.day, bd.hour, bd.minute))
    return local.utcoffset().total_seconds() / 3600.0

def julday_ut(bd: BirthData) -> float:
    ut = bd.hour + bd.minute/60.0 - tz_offset(bd)
    return swe.julday(bd.year, bd.month, bd.day, ut)

def planets_sid(jd_ut: float) -> Dict[str, float]:
    ids = {"Sun":swe.SUN,"Moon":swe.MOON,"Mars":swe.MARS,"Mercury":swe.MERCURY,
           "Jupiter":swe.JUPITER,"Venus":swe.VENUS,"Saturn":swe.SATURN,"Rahu":swe.MEAN_NODE}
    out = {k: norm(swe.calc_ut(jd_ut, pid, FLAGS_SID)[0][0]) for k, pid in ids.items()}
    out["Ketu"] = norm(out["Rahu"] + 180.0)
    return out

def sripati_madhyas_and_asc(jd_ut: float, lat: float, lon: float):
    """Sripati/Porphyry hiše po JHori, iz SIDEREAL ascmc."""
    ascmc = swe.houses_ex(jd_ut, lat, lon, b'P', FLAGS_SID)[1]
    asc = norm(ascmc[0]); mc = norm(ascmc[1])
    desc = norm(asc + 180.0); ic = norm(mc + 180.0)
    # razdeli vsak kvadrant na 3 enake dele (Porphyry)
    quads = [asc, mc, desc, ic, asc + 360.0]
    madhyas = []
    for i in range(4):
        span = (quads[i+1] - quads[i]) % 360.0
        for j in range(3):
            madhyas.append(norm(quads[i] + span * (j/3.0)))
    return madhyas, asc

def sandhis_from_madhyas(madhyas):
    """Meje bhav = sredine med sosednjima madhyama (JHora)."""
    sandhi = []
    for i in range(12):
        a = madhyas[i]; b = madhyas[(i+1) % 12]
        span = (b - a) % 360.0
        sandhi.append(norm(a + span/2.0))
    return sandhi

def house_index_for_lon(lon: float, sandhi) -> int:
    """Dodeli bhavo po intervalu sandhi[i] -> sandhi[i+1], s pravilnim wrap-aroundom."""
    L = norm(lon)
    for i in range(12):
        start = sandhi[i]
        end = sandhi[(i + 1) % 12]
        # interval [start, end) z zavijanjem čez 0°
        if end < start:
            if L >= start or L < end:
                return i + 1
        else:
            if start <= L < end:
                return i + 1
    return 12

def chara_karakas(pl):
    use = ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]
    within = {p: (30.0 - (pl[p] % 30.0) if p == "Rahu" else (pl[p] % 30.0)) for p in use}
    order = ["Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
             "Pitrukaraka","Putrakaraka","Gnatikaraka","Darakaraka"]
    ranked = sorted(use, key=lambda x: (within[x], pl[x] % 30.0), reverse=True)
    return {order[i]: {"planet": ranked[i], "sign": sign_of(pl[ranked[i]])} for i in range(8)}

# --- endpoints ---
@app.get("/health")
def health():
    ephe_ok = os.path.isdir(EPHE_PATH) and bool(os.listdir(EPHE_PATH))
    return {"ok": True, "version": APP_VERSION, "ephe_loaded": ephe_ok}

@app.post("/chart_full")
def chart_full(data: BirthData):
    jd = julday_ut(data)
    pl = planets_sid(jd)
    madhyas, asc = sripati_madhyas_and_asc(jd, data.lat, data.lon)
    sandhi = sandhis_from_madhyas(madhyas)

    planets = {
        p: {"deg": round(lon, 2), "sign": sign_of(lon), "bhava": house_index_for_lon(lon, sandhi)}
        for p, lon in pl.items()
    }

    return {
        "ayanamsa": "Lahiri",
        "house_system": "Sripati",
        "ascendant": {"deg": round(asc, 2), "sign": sign_of(asc)},
        "bhavas": {f"Bhava{i+1}": round(m, 2) for i, m in enumerate(madhyas)},    # madhya (centers)
        "bhava_sandhis": [round(x, 2) for x in sandhi],                            # meje (midpoints)
        "planets": planets,
        "chara_karakas": chara_karakas(pl)
    }

# ohrani tvoj "place" test (demo Maribor)
@app.post("/chart_place")
def chart_place(payload: Dict):
    place = payload.get("place"); dt = payload.get("datetime_local")
    if not place or not dt:
        raise HTTPException(400, "Missing 'place' or 'datetime_local'")
    if "Maribor" in place:
        lat, lon = 46.55, 15.98
    else:
        raise HTTPException(400, "Place not supported in demo chart_place")
    y, m, d = map(int, dt.split(" ")[0].split("-"))
    hh, mm = map(int, dt.split(" ")[1].split(":"))
    bd = BirthData(year=y, month=m, day=d, hour=hh, minute=mm, lat=lat, lon=lon)
    return chart_full(bd)
