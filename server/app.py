# server/app.py  — v0.4.1 (Rasi + Bhave + chart_place)
import os, sys
from typing import Optional, Dict
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import swisseph as swe
import pytz
from timezonefinder import TimezoneFinder

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# JHora-style: sidereal Lahiri + mean node
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
NAKSHATRAS = [
    "Ashvini","Bharani","Krittika","Rohini","Mrigashira","Ardra","Punarvasu",
    "Pushya","Ashlesha","Magha","Purva Phalguni","Uttara Phalguni","Hasta",
    "Chitra","Swati","Vishakha","Anuradha","Jyeshtha","Mula",
    "Purva Ashadha","Uttara Ashadha","Shravana","Dhanishtha",
    "Shatabhisha","Purva Bhadrapada","Uttara Bhadrapada","Revati"
]

APP_VERSION = "0.4.1"

class BirthData(BaseModel):
    name: Optional[str] = None
    year: int; month: int; day: int
    hour: int; minute: int
    lat: float; lon: float
    tz: Optional[float] = Field(default=None, description="UTC offset hours (auto if missing)")
    house_system: str = Field(default="P", description="Sripati/Placidus='P'")

app = FastAPI(title="JHora PyAPI", version=APP_VERSION)

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def nakshatra_of(lon_sid: float):
    idx = int((lon_sid % 360.0) / (360.0/27.0))
    pada = int(((lon_sid % (360.0/27.0)) / (360.0/108.0))) + 1
    return NAKSHATRAS[idx], pada

def tz_offset_hours(bd: BirthData) -> float:
    if bd.tz is not None:
        return float(bd.tz)
    tf = TimezoneFinder()
    tzname = tf.timezone_at(lat=bd.lat, lng=bd.lon) or "Europe/Ljubljana"
    tzinfo = pytz.timezone(tzname)
    local_naive = datetime(bd.year, bd.month, bd.day, bd.hour, bd.minute)
    localized = tzinfo.localize(local_naive, is_dst=None)
    return localized.utcoffset().total_seconds() / 3600.0

def to_julday_ut(bd: BirthData) -> float:
    ut = bd.hour + bd.minute/60.0 - tz_offset_hours(bd)
    return swe.julday(bd.year, bd.month, bd.day, ut)

def planet_longitudes_sidereal(jd_ut: float) -> Dict[str, float]:
    ids = {"Sun":swe.SUN,"Moon":swe.MOON,"Mars":swe.MARS,"Mercury":swe.MERCURY,
           "Jupiter":swe.JUPITER,"Venus":swe.VENUS,"Saturn":swe.SATURN,"Rahu":swe.MEAN_NODE}
    out = {name: swe.calc_ut(jd_ut, pid, FLAGS)[0][0] % 360.0 for name, pid in ids.items()}
    out["Ketu"] = (out["Rahu"] + 180.0) % 360.0
    return out

def ascendant_sidereal(jd_ut: float, lat: float, lon: float, hs_code: str) -> float:
    houses, ascmc = swe.houses_ex(jd_ut, lat, lon, hs_code.encode("ascii"), swe.FLG_SIDEREAL)
    return ascmc[0] % 360.0

def chara_karakas_jhora(pl: Dict[str, float]) -> Dict[str, Dict]:
    use = ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]
    within = {p: (30.0 - (pl[p] % 30.0) if p == "Rahu" else (pl[p] % 30.0)) for p in use}
    order = ["Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
             "Pitrukaraka","Putrakaraka","Gnatikaraka","Darakaraka"]
    ranked = sorted(use, key=lambda x: (within[x], pl[x] % 30.0), reverse=True)
    return {order[i]: {"planet": ranked[i], "sign": sign_of(pl[ranked[i]]), "deg": round(pl[ranked[i]], 2)} for i in range(8)}

@app.get("/health")
def health():
    ephe_ok = os.path.isdir(EPHE_PATH) and bool(os.listdir(EPHE_PATH))
    return {"ok": True, "version": APP_VERSION, "ephe": EPHE_PATH, "ephe_loaded": ephe_ok}

@app.post("/chart")
def chart(data: BirthData):
    try:
        jd = to_julday_ut(data)
        pl = planet_longitudes_sidereal(jd)
        asc = ascendant_sidereal(jd, data.lat, data.lon, data.house_system)
        moon_ns, moon_pada = nakshatra_of(pl["Moon"])
        ck = chara_karakas_jhora(pl)
        return {
            "ayanamsa": "Lahiri",
            "node": "Mean",
            "house_system": data.house_system,
            "ascendant": {"deg": round(asc, 2), "sign": sign_of(asc)},
            "planets": {k: {"deg": round(v, 2), "sign": sign_of(v)} for k, v in pl.items()},
            "moon_nakshatra": {"name": moon_ns, "pada": moon_pada},
            "chara_karakas": ck
        }
    except Exception as e:
        raise HTTPException(400, f"Calculation error: {e}")

@app.post("/chart_full")
def chart_full(data: BirthData):
    try:
        jd = to_julday_ut(data)
        pl = planet_longitudes_sidereal(jd)
        houses, ascmc = swe.houses_ex(jd, data.lat, data.lon, data.house_system.encode("ascii"), swe.FLG_SIDEREAL)

        # bhava cusps (sidereal)
        bhava_list = [h % 360.0 for h in houses]
        bhavas = {f"Bhava{i+1}": round(bhava_list[i], 2) for i in range(12)}

        # assign planet -> bhava (wrap-around safe)
        planet_houses = {}
        for p, lon in pl.items():
            bh = 12  # default
            for i in range(12):
                start = bhava_list[i]
                end = bhava_list[(i + 1) % 12]
                if end < start:
                    end += 360.0
                L = lon
                if L < start:
                    L += 360.0
                if start <= L < end:
                    bh = i + 1
                    break
            planet_houses[p] = bh

        moon_ns, moon_pada = nakshatra_of(pl["Moon"])
        ck = chara_karakas_jhora(pl)

        return {
            "ayanamsa": "Lahiri",
            "node": "Mean",
            "house_system": data.house_system,
            "ascendant": {"deg": round(ascmc[0] % 360.0, 2), "sign": sign_of(ascmc[0])},
            "bhavas": bhavas,
            "planets": {p: {"deg": round(lon, 2), "sign": sign_of(lon), "bhava": planet_houses[p]} for p, lon in pl.items()},
            "moon_nakshatra": {"name": moon_ns, "pada": moon_pada},
            "chara_karakas": ck
        }
    except Exception as e:
        raise HTTPException(400, f"Calculation error: {e}")

# Optional: ohrani “place” stil testa (Maribor demo)
@app.post("/chart_place")
def chart_place(payload: Dict):
    place = payload.get("place")
    dt = payload.get("datetime_local")
    if not place or not dt:
        raise HTTPException(400, "Missing 'place' or 'datetime_local'")
    # preprost demo geokoder: za Maribor
    if "Maribor" in place:
        lat, lon = 46.55, 15.98
    else:
        raise HTTPException(400, "Place not supported in demo chart_place")
    y, m, d = map(int, dt.split(" ")[0].split("-"))
    hr, mi = map(int, dt.split(" ")[1].split(":"))
    bd = BirthData(year=y, month=m, day=d, hour=hr, minute=mi, lat=lat, lon=lon)
    return chart_full(bd)
