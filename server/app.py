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

# ---------- Ephemeris helpers ----------
_PLANET_CODES = {
    "Sun":     swe.SUN,
    "Moon":    swe.MOON,
    "Mars":    swe.MARS,
    "Mercury": swe.MERCURY,
    "Jupiter": swe.JUPITER,
    "Venus":   swe.VENUS,
    "Saturn":  swe.SATURN,
    "Rahu":    NODE_CODE,
    "Ketu":    NODE_CODE,  # iz Rahuja +180
}

def planet_data(jd_ut: float):
    """
    Vrne:
      planets[name] = {trop_lon, trop_lat, sid_lon}
      ayan (deg)
    """
    ayan = lahiri_ayanamsa_ut(jd_ut)
    out = {}
    rahu_lon, rahu_lat = None, 0.0
    for name, code in _PLANET_CODES.items():
        if name == "Ketu":
            # 180° od Rahuja
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

def retrograde_map(jd_ut: float) -> Dict[str, bool]:
    """
    Vrne retrogradnost za Sun..Saturn. Uporabi SwissEph speed (xx[3] < 0 => retro).
    Rahu/Ketu nas tu ne zanimata (vedno False).
    """
    retro = {p: False for p in _PLANET_CODES.keys()}
    for name in ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn"]:
        code = _PLANET_CODES[name]
        # Potrebujemo speed -> FLG_SPEED
        xx, _ = swe.calc_ut(jd_ut, code, swe.FLG_SPEED)
        lon_speed = xx[3] if len(xx) >= 4 else 0.0
        retro[name] = (lon_speed < 0.0)
    retro["Rahu"] = False
    retro["Ketu"] = False
    return retro

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

# ---------- Chara Karakas (JHora accurate) ----------
def compute_chara_karakas(
    sid_lon_by_planet: Dict[str, float],
    retro_by_planet: Dict[str, bool] = None
) -> Dict[str, Dict]:
    """
    JHora 8.0 accurate Chara Karaka computation:
      - *prisiljena 8-karaka*: AK, AmK, BK, MK, PiK, PK, GK + DK=Rahu
      - Retro popravek: če je planet retrograden, stopinjo v znamenju vzamemo kot (30° - (lon % 30°))
      - Razvrščanje: po deg_in_sign DESC, nato po sid_longitude DESC (JHora tiebreak)
      - Rahu: degree-in-sign = 30° - (lon % 30°); Ketu ignoriramo
    """
    if retro_by_planet is None:
        retro_by_planet = {p: False for p in sid_lon_by_planet}

    seven = []
    for name in ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn"]:
        lon = sid_lon_by_planet[name]
        d = lon % 30.0
        if retro_by_planet.get(name, False):
            d = 30.0 - d
            if abs(d - 30.0) < 1e-12:
                d = 0.0
        seven.append({"planet": name, "deg_in_sign": d, "sid_lon": lon})

    seven.sort(key=lambda r: (r["deg_in_sign"], r["sid_lon"]), reverse=True)

    roles = [
        "Atmakaraka","Amatyakaraka","Bhratrukaraka",
        "Matrukaraka","Pitrukaraka","Putrakaraka","Gnyatikaraka"
    ]
    out: Dict[str, Dict] = {}
    for i, role in enumerate(roles):
        r = seven[i]
        out[role] = {
            "planet": r["planet"],
            "degree_in_sign": round(r["deg_in_sign"], 6),
            "sidereal_longitude": round(r["sid_lon"], 6),
            "sign": sign_of(r["sid_lon"]),
        }

    # DK = Rahu (vedno)
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

    out["_meta"] = {"scheme": "8-karaka (retro-corrected, JHora)"}
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

        # planeti
        planets, ayan = planet_data(jd)
        sid_only = {k: v["sid_lon"] for k, v in planets.items()}

        # retro status (JHora natančnost)
        retro_map = retrograde_map(jd)

        # hiše
        asc_trop, cusps_trop, planets_in_houses = placidus_houses_and_positions(
            jd, data.lat, data.lon, planets
        )

        # čara karake (JHora)
        karakas = compute_chara_karakas(sid_only, retro_map)

        # planeti izpis (trop/sid + hiša)
        planets_out = {}
        for name, v in planets.items():
            planets_out[name] = {
                "tropical": {"longitude": round(v["trop_lon"], 6), "sign": sign_of(v["trop_lon"])},
                "sidereal": {"longitude": round(v["sid_lon"], 6),  "sign": sign_of(v["sid_lon"])},
                "house": planets_in_houses[name],
                "retrograde": bool(retro_map.get(name, False))
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
