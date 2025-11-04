# server/app.py
import os, sys, math
from typing import Optional, Dict, Tuple
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from timezonefinder import TimezoneFinder
from datetime import datetime
import zoneinfo
import requests

# --- add src/ on path (repo jhora + swe) ---
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    import jhora  # noqa: F401
    import swisseph as swe
except Exception as e:
    raise RuntimeError(f"Cannot import jhora/swe: {e}")

# --- ephemerides ---
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# --- JHora 8.0 style settings ---
AYANAMSHA_MODE = swe.SIDM_LAHIRI         # Traditional Lahiri
NODE_CODE      = swe.MEAN_NODE           # Mean Node
HOUSE_SYSTEM   = b'P'                    # Sripati/Placidus (P)

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

def norm360(x: float) -> float:
    return x % 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def lahiri_ayanamsa_ut(jd_ut: float) -> float:
    swe.set_sid_mode(AYANAMSHA_MODE, 0, 0)
    return float(swe.get_ayanamsa_ut(jd_ut))

def julday_ut(y,m,d,h,mi,tz) -> float:
    ut = h + mi/60.0 - tz
    return swe.julday(y, m, d, ut)

def planet_data(jd_ut: float):
    """
    Return:
      planets[name] = {
         'trop_lon','trop_lat','sid_lon','retro'
      },  ayan (deg)
    """
    ayan = lahiri_ayanamsa_ut(jd_ut)
    bodies = {
        "Sun":     swe.SUN,
        "Moon":    swe.MOON,
        "Mars":    swe.MARS,
        "Mercury": swe.MERCURY,
        "Jupiter": swe.JUPITER,
        "Venus":   swe.VENUS,
        "Saturn":  swe.SATURN,
        "Rahu":    NODE_CODE,  # mean node
        "Ketu":    NODE_CODE,  # computed from Rahu
    }
    out = {}
    rahu_lon, rahu_lat, rahu_retro = None, 0.0, False
    for name, code in bodies.items():
        if name == "Ketu":
            trop_lon = norm360(rahu_lon + 180.0)
            sid_lon  = norm360(trop_lon - ayan)
            out["Ketu"] = {"trop_lon": trop_lon, "trop_lat": rahu_lat,
                           "sid_lon": sid_lon, "retro": rahu_retro}
            continue
        xx, _ = swe.calc_ut(jd_ut, code)
        lon_trop, lat_trop, spd = norm360(xx[0]), xx[1], xx[3]
        if name == "Rahu":
            rahu_lon, rahu_lat, rahu_retro = lon_trop, lat_trop, (spd < 0)
        out[name] = {
            "trop_lon": lon_trop,
            "trop_lat": lat_trop,
            "sid_lon":  norm360(lon_trop - ayan),
            "retro":    (spd < 0),
        }
    return out, ayan

def _houses_placidus(jd_ut: float, geolat: float, geolon: float):
    """Swiss Ephemeris Placidus cusps (tropical)."""
    try:
        hs = HOUSE_SYSTEM if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM.encode()
        cusps, ascmc = swe.houses(jd_ut, geolat, geolon, hs)
    except Exception:
        hs = HOUSE_SYSTEM.decode() if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM
        cusps, ascmc = swe.houses(jd_ut, geolat, geolon, hs)
    cusps = [norm360(c) for c in cusps[:12]]
    asc_trop = norm360(ascmc[0])
    return asc_trop, cusps

def _house_index_from_sidereal(cusps_sid, lon_sid) -> int:
    """
    Assign house by moving along SIDEREAL cusps 1..12.
    Matches JHora behavior when used with Lahiri sidereal.
    """
    L = norm360(lon_sid)
    for i in range(12):
        start = cusps_sid[i]
        end   = cusps_sid[(i+1) % 12]
        arc   = (end - start) % 360.0
        delta = (L - start) % 360.0
        if delta <= arc or math.isclose(delta, arc, rel_tol=1e-12, abs_tol=1e-8):
            return i+1
    return 12

def assign_houses_sidereal(jd_ut: float, lat: float, lon: float, ayan: float, planets):
    """
    Compute tropical cusps with Placidus, convert them to SIDEREAL (−ayan),
    then assign each planet to a house using its SIDEREAL longitude.
    """
    asc_trop, cusps_trop = _houses_placidus(jd_ut, lat, lon)
    cusps_sid = [norm360(c - ayan) for c in cusps_trop]

    houses = {}
    for name, v in planets.items():
        houses[name] = _house_index_from_sidereal(cusps_sid, v["sid_lon"])

    asc_sid = norm360(asc_trop - ayan)
    return asc_trop, asc_sid, cusps_trop, cusps_sid, houses

def compute_chara_karakas_8(sid_lon_by_planet: Dict[str,float],
                            retro_by_planet: Dict[str,bool]) -> Dict[str, Dict]:
    """
    8-karaka scheme (JHora-like):
      - candidates: Sun..Saturn + Rahu (8 bodies)
      - degree-in-sign = (lon % 30); for retrograde planets use 30 - (lon % 30)
      - Rahu degree-in-sign = 30 - (lon % 30)  (tail logic)
      - sort DESC by (deg_in_sign, sid_lon)
      - roles = [AK, AmK, BK, MK, PiK, PK, GK, DK]
    """
    def deg_in_sign(name, lon):
        d = lon % 30.0
        if name == "Rahu":               # Rahu special
            d = 30.0 - d
        elif retro_by_planet.get(name, False):
            d = 30.0 - d
        if abs(d - 30.0) < 1e-10:
            d = 0.0
        return d

    candidates = []
    for name in ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]:
        lon = sid_lon_by_planet[name]
        d   = deg_in_sign(name, lon)
        candidates.append({"planet":name, "deg":d, "lon":lon})

    candidates.sort(key=lambda r: (r["deg"], r["lon"]), reverse=True)

    roles = ["Atmakaraka","Amatyakaraka","Bhratrukaraka",
             "Matrukaraka","Pitrukaraka","Putrakaraka",
             "Gnyatikaraka","Darakaraka"]

    out = {}
    for role, item in zip(roles, candidates):
        out[role] = {
            "planet": item["planet"],
            "degree_in_sign": round(item["deg"], 6),
            "sidereal_longitude": round(item["lon"], 6),
            "sign": sign_of(item["lon"])
        }
    out["_meta"] = {"scheme": "8-karaka (Rahu in ranking, retro-corrected)"}
    return out

# --------- FastAPI ----------
app = FastAPI(title="My PyJHora API", version="0.5.0")

class BirthData(BaseModel):
    name: Optional[str] = None
    year: int; month: int; day: int
    hour: int; minute: int
    lat: float = Field(..., description="decimal degrees, N+ S-")
    lon: float = Field(..., description="decimal degrees, E+ W-")
    tz:  float = Field(..., description="timezone hours (CET=+1, CEST=+2)")

@app.get("/health")
def health():
    ok = os.path.isdir(EPHE_PATH) and len(os.listdir(EPHE_PATH)) > 0
    return {
        "status": "ok",
        "ephe_path": EPHE_PATH,
        "ephe_loaded": ok,
        "house_system": "Sripati/Placidus",
        "ayanamsa": "Lahiri",
        "node": "Mean",
    }

@app.post("/chart")
def chart(data: BirthData):
    try:
        jd = julday_ut(data.year, data.month, data.day, data.hour, data.minute, data.tz)

        # planets
        planets, ayan = planet_data(jd)
        sid_only  = {k: v["sid_lon"] for k,v in planets.items()}
        retro_map = {k: v["retro"]   for k,v in planets.items()}

        # houses (SIDEREAL assignment)
        asc_trop, asc_sid, cusps_trop, cusps_sid, houses = assign_houses_sidereal(
            jd, data.lat, data.lon, ayan, planets
        )

        # chara karakas (8-karaka with Rahu ranked; DK not fixed)
        karakas = compute_chara_karakas_8(sid_only, retro_map)

        # planets out
        planets_out = {}
        for name, v in planets.items():
            planets_out[name] = {
                "tropical": {"longitude": round(v["trop_lon"], 6), "sign": sign_of(v["trop_lon"])},
                "sidereal": {"longitude": round(v["sid_lon"], 6),  "sign": sign_of(v["sid_lon"])},
                "house": int(houses[name]),
                "retrograde": bool(v["retro"])
            }

        return {
            "name": data.name,
            "julian_day_ut": jd,
            "ayanamsa": round(ayan, 6),
            "settings": {"ayanamsa": "Lahiri", "node": "Mean", "house_system": "Sripati/Placidus"},
            "ascendant": {
                "degree_sidereal": round(asc_sid, 6),
                "sign_sidereal": sign_of(asc_sid)
            },
            "house_cusps": {
                "tropical": [round(c, 6) for c in cusps_trop],
                "sidereal": [round(c, 6) for c in cusps_sid]
            },
            "planets": planets_out,
            "chara_karakas": karakas
        }
    except Exception as e:
        raise HTTPException(400, f"Calculation error: {e}")

# ----- /chart_place: geocode + local DST -----
_tf = TimezoneFinder()

class PlaceData(BaseModel):
    place: str = Field(..., description="City, Country (e.g. 'Maribor, Slovenia')")
    datetime_local: str = Field(..., description="YYYY-MM-DD HH:MM (local time)")

def geocode_place(place: str) -> Tuple[float, float]:
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": place, "format": "json", "limit": 1}
    r = requests.get(url, params=params, headers={"User-Agent":"MyJHoraAPI/1.0"})
    if not r.ok or not r.json():
        raise HTTPException(400, f"Cannot geocode location: {place}")
    d = r.json()[0]
    return float(d["lat"]), float(d["lon"])

@app.post("/chart_place")
def chart_place(data: PlaceData):
    try:
        lat, lon = geocode_place(data.place)
        tz_name = _tf.timezone_at(lat=lat, lng=lon)
        if tz_name is None:
            raise HTTPException(400, f"Cannot find timezone for {data.place}")

        dt_local = datetime.strptime(data.datetime_local, "%Y-%m-%d %H:%M").replace(
            tzinfo=zoneinfo.ZoneInfo(tz_name)
        )
        offset_hours = dt_local.utcoffset().total_seconds() / 3600.0

        birth = BirthData(
            name=data.place,
            year=dt_local.year, month=dt_local.month, day=dt_local.day,
            hour=dt_local.hour, minute=dt_local.minute,
            lat=lat, lon=lon, tz=offset_hours
        )
        return chart(birth)
    except Exception as e:
        raise HTTPException(400, f"chart_place error: {e}")

# --- light endpoints ---
@app.post("/chart_light")
def chart_light(data: BirthData):
    full = chart(data)
    return {
        "ascendant": full["ascendant"],
        "chara_karakas": full["chara_karakas"]
    }

class PlaceDataLight(PlaceData):
    pass

@app.post("/chart_place_light")
def chart_place_light(data: PlaceDataLight):
    lat, lon = geocode_place(data.place)
    tz_name = _tf.timezone_at(lat=lat, lng=lon)
    if tz_name is None:
        raise HTTPException(400, f"Cannot find timezone for {data.place}")
    dt_local = datetime.strptime(data.datetime_local, "%Y-%m-%d %H:%M").replace(
        tzinfo=zoneinfo.ZoneInfo(tz_name)
    )
    birth = BirthData(
        name=data.place,
        year=dt_local.year, month=dt_local.month, day=dt_local.day,
        hour=dt_local.hour, minute=dt_local.minute,
        lat=lat, lon=lon, tz=dt_local.utcoffset().total_seconds()/3600.0
    )
    return chart_light(birth)
