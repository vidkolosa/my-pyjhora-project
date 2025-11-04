# server/app.py — v0.5.0 (Rasi + Bhave pravilno po Sripati/Porphyry)
import os, sys
from typing import Optional, Dict, List, Tuple
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
# JHora: sidereal Lahiri
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS_SID = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL
FLAGS_TROP = swe.FLG_SWIEPH | swe.FLG_SPEED                # brez SIDEREAL

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio",
         "Sagittarius","Capricorn","Aquarius","Pisces"]
NAKSHATRAS = ["Ashvini","Bharani","Krittika","Rohini","Mrigashira","Ardra","Punarvasu",
              "Pushya","Ashlesha","Magha","Purva Phalguni","Uttara Phalguni","Hasta",
              "Chitra","Swati","Vishakha","Anuradha","Jyeshtha","Mula",
              "Purva Ashadha","Uttara Ashadha","Shravana","Dhanishtha",
              "Shatabhisha","Purva Bhadrapada","Uttara Bhadrapada","Revati"]

APP_VERSION = "0.5.0"

class BirthData(BaseModel):
    name: Optional[str] = None
    year: int; month: int; day: int
    hour: int; minute: int
    lat: float; lon: float
    tz: Optional[float] = Field(default=None, description="UTC offset hours (auto if missing)")
    # JHora=Sṛpati → Porphyry ('O'); po želji 'P' za Placidus
    house_system: str = Field(default="O", description="Porphyry=Sripati('O'), Placidus('P')")

app = FastAPI(title="JHora PyAPI", version=APP_VERSION)

def norm(x: float) -> float:
    x %= 360.0
    return x if x >= 0 else x + 360.0

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
    local = tzinfo.localize(datetime(bd.year, bd.month, bd.day, bd.hour, bd.minute))
    return local.utcoffset().total_seconds() / 3600.0

def to_julday_ut(bd: BirthData) -> float:
    ut = bd.hour + bd.minute/60.0 - tz_offset_hours(bd)
    return swe.julday(bd.year, bd.month, bd.day, ut)

def planet_longitudes_sidereal(jd_ut: float) -> Dict[str, float]:
    ids = {"Sun":swe.SUN,"Moon":swe.MOON,"Mars":swe.MARS,"Mercury":swe.MERCURY,
           "Jupiter":swe.JUPITER,"Venus":swe.VENUS,"Saturn":swe.SATURN,"Rahu":swe.MEAN_NODE}
    out = {n: norm(swe.calc_ut(jd_ut, pid, FLAGS_SID)[0][0]) for n, pid in ids.items()}
    out["Ketu"] = norm(out["Rahu"] + 180.0)
    return out

def house_cusps_sidereal(jd_ut: float, lat: float, lon: float, hs: str) -> Tuple[List[float], float]:
    """Vrne SIDEREAL bhava 'madhya' (cusps) po izbranem sistemu + Asc SIDEREAL.
       Ključ: cuspe izračunamo tropsko in jih potem premaknemo za ayanamšo."""
    # tropski cuspi
    houses_t, ascmc_t = swe.houses_ex(jd_ut, lat, lon, hs.encode("ascii"), 0)  # brez SIDEREAL
    # ayanamša
    ay = swe.get_ayanamsa_ut(jd_ut)
    # v sidereal
    houses_s = [norm(h - ay) for h in houses_t]
    asc_s = norm(ascmc_t[0] - ay)
    return houses_s, asc_s  # vrnemo madhya (središča bhav)

def bhava_sandhis_from_madhyas(madhyas: List[float]) -> List[float]:
    """Meje bhav (sandhi) so sredine med sosednjima madhyama."""
    sandhi = []
    for i in range(12):
        a = madhyas[i]
        b = madhyas[(i + 1) % 12]
        # sredina na krogu
        diff = (b - a + 360.0) % 360.0
        mid = norm(a + diff / 2.0)
        sandhi.append(mid)
    return sandhi

def house_index_for_lon(lon: float, sandhi: List[float]) -> int:
    """Določi bhavo iz intervala sandhi[i-1] → sandhi[i]. Bhava 1 je med sandhi12→sandhi1."""
    L = norm(lon)
    for i in range(12):
        start = sandhi[i - 1] if i > 0 else sandhi[11]
        end = sandhi[i]
        # interval [start, end)
        if end < start:
            # wrap
            if L >= start or L < end:
                return i + 1
        else:
            if start <= L < end:
                return i + 1
    return 12

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
    jd = to_julday_ut(data)
    pl = planet_longitudes_sidereal(jd)
    madhyas, asc = house_cusps_sidereal(jd, data.lat, data.lon, data.house_system)
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

@app.post("/chart_full")
def chart_full(data: BirthData):
    jd = to_julday_ut(data)
    pl = planet_longitudes_sidereal(jd)
    madhyas, asc = house_cusps_sidereal(jd, data.lat, data.lon, data.house_system)
    sandhi = bhava_sandhis_from_madhyas(madhyas)

    bhavas = {f"Bhava{i+1}": round(madhyas[i], 2) for i in range(12)}
    planets = {
        p: {"deg": round(lon, 2), "sign": sign_of(lon), "bhava": house_index_for_lon(lon, sandhi)}
        for p, lon in pl.items()
    }
    moon_ns, moon_pada = nakshatra_of(pl["Moon"])
    ck = chara_karakas_jhora(pl)

    return {
        "ayanamsa": "Lahiri",
        "node": "Mean",
        "house_system": data.house_system,
        "ascendant": {"deg": round(asc, 2), "sign": sign_of(asc)},
        "bhavas": bhavas,                      # bhava madhya (cusps)
        "bhava_sandhis": [round(x,2) for x in sandhi],  # meje hiš
        "planets": planets,                    # zdaj pravilno po bhavah
        "moon_nakshatra": {"name": moon_ns, "pada": moon_pada},
        "chara_karakas": ck
    }

# Ohranimo tvoj stari test format (Maribor demo)
@app.post("/chart_place")
def chart_place(payload: Dict):
    place = payload.get("place")
    dt = payload.get("datetime_local")
    if not place or not dt:
        raise HTTPException(400, "Missing 'place' or 'datetime_local'")
    if "Maribor" in place:
        lat, lon = 46.55, 15.98
    else:
        raise HTTPException(400, "Place not supported in demo chart_place")
    y, m, d = map(int, dt.split(" ")[0].split("-"))
    hr, mi = map(int, dt.split(" ")[1].split(":"))
    bd = BirthData(year=y, month=m, day=d, hour=hr, minute=mi, lat=lat, lon=lon, house_system="O")
    return chart_full(bd)
