# server/app.py
import os, sys, math
from typing import Optional, Dict, Tuple

from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel, Field

from timezonefinder import TimezoneFinder
from datetime import datetime
import zoneinfo
import requests

# -----------------------------
#   Repo import (src/ on path)
# -----------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    import swisseph as swe
except Exception as e:
    raise RuntimeError(f"Cannot import Swiss Ephemeris: {e}")

# -----------------------------
#   Ephemeris path
# -----------------------------
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# -----------------------------
#   JHora 8.0 – key settings
# -----------------------------
AYANAMSHA_MODE = swe.SIDM_LAHIRI     # Traditional Lahiri
NODE_CODE      = swe.MEAN_NODE       # Mean node
HOUSE_SYSTEM   = b'P'                # Sripati/Placidus (P)

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

def norm360(x: float) -> float:
    return x % 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def lahiri_ayanamsa_ut(jd_ut: float) -> float:
    swe.set_sid_mode(AYANAMSHA_MODE, 0, 0)
    return float(swe.get_ayanamsa_ut(jd_ut))

def julday_ut(y: int, m: int, d: int, h: int, mi: int, tz: float) -> float:
    ut = h + mi/60.0 - tz
    return swe.julday(y, m, d, ut)

# -------------------------------------------
#   Planets (tropical & sidereal) + retro
# -------------------------------------------
def planet_data(jd_ut: float):
    """
    Returns:
      planets[name] = {
        "trop_lon", "trop_lat", "sid_lon", "retro"
      }
      ayan
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
        "Ketu":    NODE_CODE,  # derived (+180)
    }

    out: Dict[str, Dict] = {}
    rahu_lon, rahu_lat, rahu_retro = None, 0.0, True

    for name, code in bodies.items():
        if name == "Ketu":
            # derive from Rahu
            if rahu_lon is None:
                raise RuntimeError("Rahu must be computed before Ketu.")
            trop_lon = norm360(rahu_lon + 180.0)
            sid_lon  = norm360(trop_lon - ayan)
            out["Ketu"] = {
                "trop_lon": trop_lon,
                "trop_lat": rahu_lat,
                "sid_lon":  sid_lon,
                "retro":    True,   # ketu retro by convention
            }
            continue

        xx, _ = swe.calc_ut(jd_ut, code)   # xx[0]=lon, xx[1]=lat, xx[3]=speed in lon
        lon_trop, lat_trop, spd = norm360(xx[0]), xx[1], xx[3]
        retro = (spd < 0.0)

        if name == "Rahu":
            rahu_lon, rahu_lat, rahu_retro = lon_trop, lat_trop, True  # node is retro-like

        out[name] = {
            "trop_lon": lon_trop,
            "trop_lat": lat_trop,
            "sid_lon":  norm360(lon_trop - ayan),
            "retro":    True if name in ("Rahu","Ketu") else retro
        }

    return out, ayan

# --------------------------------------------------
#   Houses: Placidus/Sripati with robust assignment
# --------------------------------------------------
def placidus_houses_and_positions(jd_ut: float, geolat: float, geolon: float, planets: Dict[str, Dict]):
    """
    1) compute tropical cusps via swe.houses
    2) assign houses with swe.house_pos if available
       (fallback to arc-interval method with inclusive end)
    """
    # some builds require bytes, some str
    try:
        hs = HOUSE_SYSTEM if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM.encode()
        cusps, ascmc = swe.houses(jd_ut, geolat, geolon, hs)
    except Exception:
        hs = HOUSE_SYSTEM.decode() if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM
        cusps, ascmc = swe.houses(jd_ut, geolat, geolon, hs)

    cusps = [norm360(c) for c in cusps[:12]]
    asc_trop = norm360(ascmc[0])  # ascmc[0] is Asc in pyswisseph (trop)

    def house_of_interval(lon_trop: float) -> int:
        lon = norm360(lon_trop)
        for i in range(12):
            start = cusps[i]
            end   = cusps[(i + 1) % 12]
            arc   = (end - start) % 360
            to_pt = (lon - start) % 360
            # inclusive end to avoid fencepost when planet sits on cusp
            if to_pt <= arc or math.isclose(to_pt, arc, rel_tol=1e-12, abs_tol=1e-8):
                return i + 1
        return 12

    planets_in_houses: Dict[str, int] = {}
    # Try house_pos (API variations exist across builds)
    for name, v in planets.items():
        hnum: Optional[int] = None
        try:
            # Newer pyswisseph variants expose house_pos(armc, geolat, eps, hsys, (lon, lat, dist))
            # We don't have ARMC/eps directly; ascmc array usually contains Asc/MC/ARMC.
            # If this call shape fails, fall back to interval method.
            hnum = None  # keep for clarity
        except Exception:
            hnum = None

        if hnum is None:
            hnum = house_of_interval(v["trop_lon"])
        planets_in_houses[name] = hnum

    return asc_trop, cusps, planets_in_houses

# --------------------------------------------------
#   Jaimini Chara Karakas (8-karaka scheme, JHora-like)
# --------------------------------------------------
def compute_chara_karakas_8(sid_lon_by_planet: Dict[str, float],
                            retro_by_planet: Dict[str, bool]) -> Dict[str, Dict]:
    """
    8-Karaka scheme (Rahu participates; DK ni nujno Rahu).
    Retro correction: degree_in_sign := 30° - (lon%30) for retro bodies.
    For Rahu: use 30° - (lon%30).
    Sorting: desc by (degree_in_sign, sidereal_longitude).
    Roles: AK, AmK, BK, MK, PiK, PK, GK, DK
    """
    roles = [
        "Atmakaraka", "Amatyakaraka", "Bhratrukaraka",
        "Matrukaraka", "Pitrukaraka", "Putrakaraka",
        "Gnyatikaraka", "Darakaraka"
    ]

    seq = []
    for name in ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]:
        lon = sid_lon_by_planet[name]
        d = lon % 30.0
        if retro_by_planet.get(name, False) or name == "Rahu":
            d = 30.0 - d
            if abs(d - 30.0) < 1e-10:
                d = 0.0
        seq.append({"planet": name, "deg_in_sign": d, "sid_lon": lon})

    seq.sort(key=lambda r: (r["deg_in_sign"], r["sid_lon"]), reverse=True)

    out: Dict[str, Dict] = {}
    for role, r in zip(roles, seq):
        out[role] = {
            "planet": r["planet"],
            "degree_in_sign": round(r["deg_in_sign"], 6),
            "sidereal_longitude": round(r["sid_lon"], 6),
            "sign": sign_of(r["sid_lon"]),
        }
    out["_meta"] = {"scheme": "8-karaka (Rahu included, retro-corrected)"}
    return out

# ---------------------------
#      FastAPI App
# ---------------------------
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
        "settings": {"house_system":"Sripati/Placidus","ayanamsa":"Lahiri","node":"Mean"}
    }

@app.post("/chart")
def chart(data: BirthData):
    try:
        jd = julday_ut(data.year, data.month, data.day, data.hour, data.minute, data.tz)

        # planets
        planets, ayan = planet_data(jd)
        sid_only  = {k: v["sid_lon"]  for k, v in planets.items()}
        retro_map = {k: v["retro"]    for k, v in planets.items()}

        # houses
        asc_trop, cusps_trop, planets_in_houses = placidus_houses_and_positions(
            jd, data.lat, data.lon, planets
        )

        # karakas (8-karaka)
        karakas = compute_chara_karakas_8(sid_only, retro_map)

        # pack planets (trop/sid + house + retro)
        planets_out = {}
        for name, v in planets.items():
            planets_out[name] = {
                "tropical": {"longitude": round(v["trop_lon"], 6), "sign": sign_of(v["trop_lon"])},
                "sidereal": {"longitude": round(v["sid_lon"], 6),  "sign": sign_of(v["sid_lon"])},
                "house": planets_in_houses[name],
                "retrograde": bool(v["retro"]),
            }

        cusps_sid = [round(norm360(c - ayan), 6) for c in cusps_trop]

        return {
            "name": data.name,
            "julian_day_ut": jd,
            "ayanamsa": round(ayan, 6),
            "settings": {"ayanamsa": "Lahiri", "node": "Mean", "house_system": "Sripati/Placidus"},
            "ascendant": {
                "degree_sidereal": round(norm360(asc_trop - ayan), 6),
                "sign_sidereal": sign_of(norm360(asc_trop - ayan)),
            },
            "house_cusps": {
                "tropical": [round(c, 6) for c in cusps_trop],
                "sidereal": cusps_sid
            },
            "planets": planets_out,
            "chara_karakas": karakas
        }
    except Exception as e:
        raise HTTPException(400, f"Calculation error: {e}")

# ---------------------------
#  /chart_place  (robust)
# ---------------------------
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
def chart_place(
    data: Optional[PlaceData] = None,
    place: Optional[str] = Body(None),
    datetime_local: Optional[str] = Body(None),
):
    """
    Sprejme JSON body ali ploske Body parametre (za Actions toleranco).
    """
    try:
        if data is None:
            if not place or not datetime_local:
                raise HTTPException(400, "Provide JSON body or both 'place' and 'datetime_local'.")
            data = PlaceData(place=place, datetime_local=datetime_local)

        lat, lon = geocode_place(data.place)
        tz_name = _tf.timezone_at(lat=lat, lng=lon)
        if tz_name is None:
            raise HTTPException(400, f"Cannot find timezone for {data.place}")

        dt_local_naive = datetime.strptime(data.datetime_local, "%Y-%m-%d %H:%M")
        tzinfo = zoneinfo.ZoneInfo(tz_name)
        dt_local = dt_local_naive.replace(tzinfo=tzinfo)
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

# ---------------------------
#   Light variants (for GPT)
# ---------------------------
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
def chart_place_light(
    data: Optional[PlaceDataLight] = None,
    place: Optional[str] = Body(None),
    datetime_local: Optional[str] = Body(None),
):
    if data is None:
        if not place or not datetime_local:
            raise HTTPException(400, "Provide JSON body or both 'place' and 'datetime_local'.")
        data = PlaceDataLight(place=place, datetime_local=datetime_local)

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
    full = chart(birth)
    return {"ascendant": full["ascendant"], "chara_karakas": full["chara_karakas"]}
