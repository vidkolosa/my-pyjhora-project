# server/app.py
import os, sys, math
from typing import Optional, Dict, Tuple
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from timezonefinder import TimezoneFinder
from datetime import datetime
import zoneinfo
import requests

# --- pot do src/ da uvozimo jhora/swe iz tvojega repoja ---
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    import jhora  # noqa: F401  (zato da je paket na poti)
    import swisseph as swe
except Exception as e:
    raise RuntimeError(f"Cannot import jhora/swe: {e}")

# --- EPHE ---
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# --- Nastavitve: kot JHora 8.0 ---
AYANAMSHA_MODE = swe.SIDM_LAHIRI  # Lahiri
NODE_CODE      = swe.MEAN_NODE    # mean node
HOUSE_SYSTEM   = b'P'             # Sripati/Placidus (P)

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
    Vrne:
      planets[name] = {trop_lon, trop_lat, sid_lon}
      ayan (deg)
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
        "Ketu":    NODE_CODE,  # iz Rahuja +180
    }
    out = {}
    rahu_lon, rahu_lat = None, 0.0
    for name, code in bodies.items():
        if name == "Ketu":
            trop_lon = norm360(rahu_lon + 180.0)
            sid_lon  = norm360(trop_lon - ayan)
            out["Ketu"] = {"trop_lon": trop_lon, "trop_lat": rahu_lat, "sid_lon": sid_lon}
            continue
        xx, _ = swe.calc_ut(jd_ut, code)
        lon_trop, lat_trop = norm360(xx[0]), xx[1]
        if name == "Rahu":
            rahu_lon, rahu_lat = lon_trop, lat_trop
        out[name] = {
            "trop_lon": lon_trop,
            "trop_lat": lat_trop,
            "sid_lon":  norm360(lon_trop - ayan),
        }
    return out, ayan

def placidus_houses_and_positions(jd_ut: float, geolat: float, geolon: float, planets):
    """
    Placidus (Sripati) hiše in dodelitev planetov po intervalih med cuspami.
    Stabilno čez vse različice pyswisseph (ne uporablja house_pos).
    """
    # varno kliči swe.houses (nekje zahteva bytes 'P', drugje str)
    try:
        hs = HOUSE_SYSTEM if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM.encode()
        cusps, ascmc = swe.houses(jd_ut, geolat, geolon, hs)
    except Exception:
        hs = HOUSE_SYSTEM.decode() if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM
        cusps, ascmc = swe.houses(jd_ut, geolat, geolon, hs)

    cusps = [norm360(c) for c in cusps[:12]]  # tropični cusp-i
    asc_trop = norm360(ascmc[0])

    def house_of(lon_trop: float) -> int:
        lon = norm360(lon_trop)
        for i in range(12):
            start = cusps[i]
            end   = cusps[(i + 1) % 12]
            arc   = (end - start) % 360
            to_pt = (lon - start) % 360
            if to_pt <= arc or math.isclose(to_pt, arc, rel_tol=1e-12, abs_tol=1e-8):
                return i + 1
        return 12

    planets_in_houses = {name: house_of(v["trop_lon"]) for name, v in planets.items()}
    return asc_trop, cusps, planets_in_houses



def compute_chara_karakas(sid_lon_by_planet: Dict[str, float]) -> Dict[str, Dict]:
    """
    JHora-style *forced 8-karaka*:
      - rangiramo 7 grah (Sun..Saturn) po degree-in-sign (desc), tie-break po lon (desc)
      - razdelimo vloge: AK, AmK, BK, MK, PiK, PK, GK
      - Darakaraka = vedno Rahu (mean node)
      - Ketu je vedno izključen
    To se ujema z izpisom, kot ga imaš v JHora (Sun=PiK, PK=Jupiter, GK=Mercury, DK=Rahu).
    """
    seven = []
    for name in ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn"]:
        lon = sid_lon_by_planet[name]
        d = lon % 30.0
        seven.append({"planet": name, "deg_in_sign": d, "sid_lon": lon})

    # višja stopnja v znamenju -> višji karaka; ob enakosti večja sid. dolžina
    seven.sort(key=lambda r: (r["deg_in_sign"], r["sid_lon"]), reverse=True)

    roles = ["Atmakaraka","Amatyakaraka","Bhratrukaraka",
             "Matrukaraka","Pitrukaraka","Putrakaraka","Gnyatikaraka"]

    out: Dict[str, Dict] = {}
    for i, role in enumerate(roles):
        r = seven[i]
        out[role] = {
            "planet": r["planet"],
            "degree_in_sign": round(r["deg_in_sign"], 6),
            "sidereal_longitude": round(r["sid_lon"], 6),
            "sign": sign_of(r["sid_lon"]),
        }

    # DK = Rahu
    rahu_lon = sid_lon_by_planet["Rahu"]
    rahu_d = 30.0 - (rahu_lon % 30.0)
    if abs(rahu_d - 30.0) < 1e-12:
        rahu_d = 0.0
    out["Darakaraka"] = {
        "planet": "Rahu",
        "degree_in_sign": round(rahu_d, 6),
        "sidereal_longitude": round(rahu_lon, 6),
        "sign": sign_of(rahu_lon),
    }

    out["_meta"] = {"scheme": "8-karaka (forced JHora setting)"}
    return out






# --------- FastAPI ----------
app = FastAPI(title="My PyJHora API", version="0.4.0")

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

        # planeti
        planets, ayan = planet_data(jd)
        sid_only = {k: v["sid_lon"] for k, v in planets.items()}

        # hiše
        asc_trop, cusps_trop, planets_in_houses = placidus_houses_and_positions(
            jd, data.lat, data.lon, planets
        )

        # čara karake (JHora)
        karakas = compute_chara_karakas(sid_only)

        # planeti izpis (trop/sid + hiša)
        planets_out = {}
        for name, v in planets.items():
            planets_out[name] = {
                "tropical": {"longitude": round(v["trop_lon"], 6), "sign": sign_of(v["trop_lon"])},
                "sidereal": {"longitude": round(v["sid_lon"], 6),  "sign": sign_of(v["sid_lon"])},
                "house": planets_in_houses[name],
            }

        # cusps: tropični in sideralni (Lahiri)
        cusps_sid = [round(norm360(c - ayan), 6) for c in cusps_trop]

        return {
            "name": data.name,
            "julian_day_ut": jd,
            "ayanamsa": round(ayan, 6),
            "settings": {"ayanamsa": "Lahiri", "node": "Mean", "house_system": "Sripati/Placidus"},
            # JHora skladno: vrni samo SID asc (Lagna)
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

# ----- /chart_place: kraj + lokalni čas (sam izračuna lat/lon/DST) -----
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

        # lokalni čas (aware)
        dt_local_naive = datetime.strptime(data.datetime_local, "%Y-%m-%d %H:%M")
        tzinfo = zoneinfo.ZoneInfo(tz_name)
        dt_local = dt_local_naive.replace(tzinfo=tzinfo)

        offset_hours = dt_local.utcoffset().total_seconds() / 3600.0

        birth = BirthData(
            name=data.place,
            year=dt_local.year,
            month=dt_local.month,
            day=dt_local.day,
            hour=dt_local.hour,
            minute=dt_local.minute,
            lat=lat,
            lon=lon,
            tz=offset_hours
        )
        return chart(birth)
    except Exception as e:
        raise HTTPException(400, f"chart_place error: {e}")

# --- Lite različice za GPT (majhen JSON) ---

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
    # ista logika kot /chart_place, samo vrnemo light
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

