# server/app.py
import os, sys, math
from typing import Optional, Dict, Tuple, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from timezonefinder import TimezoneFinder
from datetime import datetime
import zoneinfo
import requests

# --- repo path (src/) ---
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# --- Swiss Ephemeris / jhora ---
try:
    import jhora  # noqa: F401 (ensure package is importable)
    import swisseph as swe
except Exception as e:
    raise RuntimeError(f"Cannot import jhora/swe: {e}")

# --- Ephemerides path ---
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# --- JHora 8.0 style settings ---
AYANAMSHA_MODE = swe.SIDM_LAHIRI           # Traditional Lahiri
NODE_CODE      = swe.MEAN_NODE             # Mean node
HOUSE_SYSTEM   = b'P'                      # Sripati/Placidus

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

def norm360(x: float) -> float:
    return x % 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def lahiri_ayanamsa_ut(jd_ut: float) -> float:
    swe.set_sid_mode(AYANAMSHA_MODE, 0, 0)
    return float(swe.get_ayanamsa_ut(jd_ut))

def julday_ut(y: int, m: int, d: int, h: int, mi: int, tz_hours: float) -> float:
    ut = h + mi/60.0 - tz_hours
    return swe.julday(y, m, d, ut)

# ---------- planets ----------
def planet_data(jd_ut: float):
    """
    Returns:
      planets[name] = {
        'trop_lon', 'trop_lat', 'sid_lon', 'retro' (bool)
      }
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
        "Rahu":    NODE_CODE,
        "Ketu":    NODE_CODE,  # derived
    }
    out: Dict[str, Dict] = {}
    rahu_lon, rahu_lat, rahu_retro = None, 0.0, False

    for name, code in bodies.items():
        if name == "Ketu":
            # opposite Rahu
            trop_lon = norm360(rahu_lon + 180.0)
            sid_lon  = norm360(trop_lon - ayan)
            out["Ketu"] = {
                "trop_lon": trop_lon,
                "trop_lat": rahu_lat,
                "sid_lon":  sid_lon,
                "retro":    rahu_retro  # flag ni pomemben, samo za info
            }
            continue

        xx, _ = swe.calc_ut(jd_ut, code)
        lon_trop, lat_trop = norm360(xx[0]), xx[1]
        retro = bool(xx[3] < 0)  # negative speed -> retrograde

        if name == "Rahu":
            rahu_lon, rahu_lat, rahu_retro = lon_trop, lat_trop, retro

        out[name] = {
            "trop_lon": lon_trop,
            "trop_lat": lat_trop,
            "sid_lon":  norm360(lon_trop - ayan),
            "retro":    retro
        }
    return out, ayan

# ---------- houses (Sripati / Placidus) ----------
def _short_arc(a: float, b: float) -> float:
    """Forward arc from a -> b in [0,360)."""
    return (b - a) % 360.0

def _midpoint(a: float, b: float) -> float:
    """Midpoint along the *shortest* arc from a to b (both in [0,360))."""
    a = norm360(a); b = norm360(b)
    arc = _short_arc(a, b)
    return norm360(a + arc/2.0)

def placidus_houses_and_positions(
    jd_ut: float, geolat: float, geolon: float, planets: Dict[str, Dict]
):
    """
    JHora-kompatibilno:
      - cusps iz swe.houses so *središča* hiš
      - meje (bhava-sandhi) so midpoints med sosednjimi cusp-i (po najkrajšem loku)
      - dodelitev hiš: točka spada v hišo i, če je v loku (boundary_{i-1}, boundary_i]
    """
    try:
        hs = HOUSE_SYSTEM if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM.encode()
        cusps_raw, ascmc = swe.houses(jd_ut, geolat, geolon, hs)
    except Exception:
        hs = HOUSE_SYSTEM.decode() if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM
        cusps_raw, ascmc = swe.houses(jd_ut, geolat, geolon, hs)

    cusps: List[float] = [norm360(c) for c in cusps_raw[:12]]  # tropical cusp-middles
    asc_trop = norm360(ascmc[0])

    # boundaries: midpoint between cusp i and cusp i+1
    boundaries = []
    for i in range(12):
        c_i   = cusps[i]
        c_ip1 = cusps[(i + 1) % 12]
        boundaries.append(_midpoint(c_i, c_ip1))  # boundary_i is end of house i

    def _in_forward_arc(start: float, end: float, x: float) -> bool:
        """Check x in (start, end] along forward direction."""
        start = norm360(start); end = norm360(end); x = norm360(x)
        arc = _short_arc(start, end)
        dx  = _short_arc(start, x)
        # include end, exclude start (JHora je tolerantna okoli epsilonov)
        return (0.0 < dx) and (dx <= arc or math.isclose(dx, arc, rel_tol=1e-12, abs_tol=1e-8))

    def house_of(lon_trop: float) -> int:
        # house i (1..12): (boundary_{i-1}, boundary_i]
        for i in range(12):
            start = boundaries[(i - 1) % 12]
            end   = boundaries[i]
            if _in_forward_arc(start, end, lon_trop):
                return i + 1
        return 12  # fallback

    planets_in_houses = {name: house_of(v["trop_lon"]) for name, v in planets.items()}
    return asc_trop, cusps, boundaries, planets_in_houses

# ---------- Chara Karakas (8-karaka incl. Rahu) ----------
def compute_chara_karakas(
    sid_lon_by_planet: Dict[str, float],
    retro_by_planet: Dict[str, bool]
) -> Dict[str, Dict]:
    """
    JHora 8-karaka shema z retro pravilom:
      - Za Sun..Saturn: d = lon%30; če retro -> d = 30 - d
      - Za Rahu: d = 30 - (lon%30) (ne glede na retro)
      - Razvrsti po (d, sid_lon) padajoče
      - Dodeli: AK, AmK, BK, MK, PiK, PK, GK, DK
    """
    order: List[Dict] = []

    for name in ["Sun", "Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn"]:
        lon = sid_lon_by_planet[name]
        d = lon % 30.0
        if retro_by_planet.get(name, False):
            d = 30.0 - d
            if abs(d - 30.0) < 1e-10:
                d = 0.0
        order.append({"planet": name, "deg": d, "sid_lon": lon})

    # Rahu
    rlon = sid_lon_by_planet["Rahu"]
    rd = 30.0 - (rlon % 30.0)
    if abs(rd - 30.0) < 1e-10:
        rd = 0.0
    order.append({"planet": "Rahu", "deg": rd, "sid_lon": rlon})

    # sort: higher degree-in-sign first; tie-break by higher longitude
    order.sort(key=lambda r: (r["deg"], r["sid_lon"]), reverse=True)

    roles = [
        "Atmakaraka", "Amatyakaraka", "Bhratrukaraka",
        "Matrukaraka", "Pitrukaraka", "Putrakaraka", "Gnyatikaraka",
        "Darakaraka"
    ]

    out: Dict[str, Dict] = {}
    for i, role in enumerate(roles):
        r = order[i]
        out[role] = {
            "planet": r["planet"],
            "degree_in_sign": round(r["deg"], 6),
            "sidereal_longitude": round(r["sid_lon"], 6),
            "sign": sign_of(r["sid_lon"]),
        }
    out["_meta"] = {"scheme": "8-karaka (JHora-style, Rahu included, retro corrected)"}
    return out

# ---------- FastAPI ----------
app = FastAPI(title="My PyJHora API", version="0.6.0")

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

        # planets (tropical/sidereal + retro)
        planets, ayan = planet_data(jd)
        sid_only  = {k: v["sid_lon"] for k, v in planets.items()}
        retro_map = {k: v["retro"]   for k, v in planets.items()}

        # houses (Sripati)
        asc_trop, cusps_trop, boundaries, planets_in_houses = placidus_houses_and_positions(
            jd, data.lat, data.lon, planets
        )

        # karakas (JHora 8-karaka, Rahu allowed as AK)
        karakas = compute_chara_karakas(sid_only, retro_map)

        # output planets
        planets_out = {}
        for name, v in planets.items():
            planets_out[name] = {
                "tropical": {"longitude": round(v["trop_lon"], 6), "sign": sign_of(v["trop_lon"])},
                "sidereal": {"longitude": round(v["sid_lon"], 6),  "sign": sign_of(v["sid_lon"])},
                "retrograde": retro_map[name],
                "house": planets_in_houses[name],
            }

        cusps_sid = [round(norm360(c - ayan), 6) for c in cusps_trop]
        bounds_sid = [round(norm360(b - ayan), 6) for b in boundaries]

        return {
            "name": data.name,
            "julian_day_ut": jd,
            "ayanamsa": round(ayan, 6),
            "settings": {"ayanamsa": "Lahiri", "node": "Mean", "house_system": "Sripati/Placidus"},
            # JHora style: vrni samo SID asc (Lagna)
            "ascendant": {
                "degree_sidereal": round(norm360(asc_trop - ayan), 6),
                "sign_sidereal": sign_of(norm360(asc_trop - ayan)),
            },
            "house_cusps": {
                "tropical_cusps_mid": [round(c, 6) for c in cusps_trop],
                "sidereal_cusps_mid": cusps_sid,
                "tropical_boundaries": [round(b, 6) for b in boundaries],
                "sidereal_boundaries": bounds_sid
            },
            "planets": planets_out,
            "chara_karakas": karakas
        }
    except Exception as e:
        raise HTTPException(400, f"Calculation error: {e}")

# ----- /chart_place: place + local time (auto lat/lon + DST) -----
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
        tz_hours = dt_local.utcoffset().total_seconds() / 3600.0

        birth = BirthData(
            name=data.place,
            year=dt_local.year, month=dt_local.month, day=dt_local.day,
            hour=dt_local.hour, minute=dt_local.minute,
            lat=lat, lon=lon, tz=tz_hours
        )
        return chart(birth)
    except Exception as e:
        raise HTTPException(400, f"chart_place error: {e}")

# --- "light" endpoints for smaller JSONs (za GPT) ---
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
