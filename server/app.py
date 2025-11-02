import os, sys, math
from typing import Optional, Dict, Tuple
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# dodatno za /chart_place
from timezonefinder import TimezoneFinder
from datetime import datetime
import zoneinfo
import requests

# --- import iz src/ ---
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    import jhora
    import swisseph as swe
except Exception as e:
    raise RuntimeError(f"Cannot import jhora: {e}")

EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

# Fiksno delovanje kot JHora 8.0
AYANAMSHA_MODE = swe.SIDM_LAHIRI     # Lahiri
NODE_CODE      = swe.MEAN_NODE       # mean node
HOUSE_SYSTEM   = b'P'                # Sripati/Placidus

# ---------- util ----------
SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

def norm360(x: float) -> float:
    return x % 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def deg_in_sign(lon: float) -> float:
    return lon % 30.0

def lahiri_ayanamsa_ut(jd_ut: float) -> float:
    swe.set_sid_mode(AYANAMSHA_MODE, 0, 0)
    return float(swe.get_ayanamsa_ut(jd_ut))

def to_sidereal(lon_tropical: float, ayan: float) -> float:
    return norm360(lon_tropical - ayan)

def julday_ut(y,m,d,h,mi,tz) -> float:
    ut = h + mi/60.0 - tz
    return swe.julday(y, m, d, ut)

def planet_data(jd_ut: float):
    """
    Vrne dict:
      name -> {trop_lon, trop_lat, sid_lon}
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
        "Ketu":    NODE_CODE,  # iz Rahuja +180
    }

    out = {}
    rahu_lon = None
    rahu_lat = 0.0
    for name, code in bodies.items():
        if name == "Ketu":
            # izračun iz Rahuja
            trop_lon = norm360(rahu_lon + 180.0)
            sid_lon  = to_sidereal(trop_lon, ayan)
            out["Ketu"] = {"trop_lon": trop_lon, "trop_lat": rahu_lat, "sid_lon": sid_lon}
            continue

        xx, _ = swe.calc_ut(jd_ut, code)
        lon_trop, lat_trop = xx[0], xx[1]
        if name == "Rahu":
            rahu_lon, rahu_lat = lon_trop, lat_trop
        out[name] = {
            "trop_lon": norm360(lon_trop),
            "trop_lat": lat_trop,
            "sid_lon":  to_sidereal(lon_trop, ayan),
        }
    return out, ayan

def compute_chara_karakas(sid_lon_by_planet: Dict[str, float]) -> Dict[str, Dict]:
    # 7-karaka shema; Ketu izključen; Rahu: 30° - degree_in_sign
    ranking = []
    for name, lon in sid_lon_by_planet.items():
        if name == "Ketu":
            continue
        d = deg_in_sign(lon)
        if name == "Rahu":
            d = 30.0 - d
            if abs(d - 30.0) < 1e-8:
                d = 0.0
        ranking.append((name, d, lon))
    ranking.sort(key=lambda x: (x[1], x[2]), reverse=True)

    order = ["Atmakaraka","Amatyakaraka","Bhratrukaraka",
             "Matrukaraka","Putrakaraka","Gnyatikaraka","Darakaraka"]
    karakas = {}
    for i, role in enumerate(order):
        if i < len(ranking):
            name, d, lon = ranking[i]
            karakas[role] = {
                "planet": name,
                "degree_in_sign": round(d, 4),
                "sidereal_longitude": round(lon, 6),
                "sign": sign_of(lon),
            }
    return karakas

def placidus_houses_and_positions(jd_ut: float, geolat: float, geolon: float, planets):
    """
    Placidus hiše (Sripati) in dodelitev bhav planetom.
    Podpira vse tri možne podpise funkcije house_pos:
    (5), (6) ali (7) argumentov.
    """
    # True obliquity
    ecl = swe.calc_ut(jd_ut, swe.ECL_NUT)[0]
    eps_true = ecl[1]

    # ARMC iz lokalnega sideričnega časa
    gst_hours = swe.sidtime(jd_ut)
    lst_hours = gst_hours + geolon / 15.0
    armc = (lst_hours % 24.0) * 15.0

    # Placidus hiše
    cusps, ascmc = swe.houses_armc(armc, geolat, eps_true, HOUSE_SYSTEM)
    asc = ascmc[0]

    planets_in_houses = {}
    for name, data in planets.items():
        lon = data["trop_lon"]
        lat = data["trop_lat"]
        try:
            # najnovejša verzija (7 arg)
            hpos = swe.house_pos(armc, geolat, eps_true, HOUSE_SYSTEM, lon, lat, 1.0)
        except TypeError:
            try:
                # srednja verzija (6 arg)
                hpos = swe.house_pos(armc, geolat, eps_true, HOUSE_SYSTEM, lon, lat)
            except TypeError:
                # najstarejša verzija (5 arg)
                hpos = swe.house_pos(armc, geolat, eps_true, lon, lat)
        # varnostne meje
        house_num = int(hpos) if 1 <= int(hpos) <= 12 else ((int(hpos) - 1) % 12 + 1)
        planets_in_houses[name] = house_num

    return asc, cusps, planets_in_houses


# ---------- API ----------
app = FastAPI(title="My PyJHora API", version="0.3.0")

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
    return {"status":"ok", "ephe_path": EPHE_PATH, "ephe_loaded": ok,
            "house_system": "Sripati/Placidus", "ayanamsa": "Lahiri", "node": "Mean"}

@app.post("/chart")
def chart(data: BirthData):
    try:
        jd = julday_ut(data.year, data.month, data.day, data.hour, data.minute, data.tz)

        # planeti
        planets, ayan = planet_data(jd)
        sid_only = {k: v["sid_lon"] for k,v in planets.items()}

        # hiše (vedno Placidus, kot v JHora)
        asc, cusps, planets_in_houses = placidus_houses_and_positions(
            jd, data.lat, data.lon, planets
        )

        # čara karake
        karakas = compute_chara_karakas(sid_only)

        # lep izpis planetov (trop + sid + hiša)
        planets_out = {}
        for name, v in planets.items():
            planets_out[name] = {
                "tropical": {"longitude": round(v["trop_lon"], 6), "sign": sign_of(v["trop_lon"])},
                "sidereal": {"longitude": round(v["sid_lon"], 6),  "sign": sign_of(v["sid_lon"])},
                "house": planets_in_houses[name]
            }

        # cusps
        cusps_sid = [round(norm360(c - ayan), 6) for c in cusps]

        return {
            "name": data.name,
            "julian_day_ut": jd,
            "ayanamsa": round(ayan, 6),
            "settings": {"ayanamsa": "Lahiri", "node":"Mean", "house_system":"Sripati/Placidus"},
            "ascendant": {"degree_tropical": round(asc, 6), "sign_tropical": sign_of(asc),
                          "degree_sidereal": round(norm360(asc - ayan), 6), "sign_sidereal": sign_of(norm360(asc - ayan))},
            "house_cusps": {
                "tropical": [round(c, 6) for c in cusps],
                "sidereal": cusps_sid
            },
            "planets": planets_out,
            "chara_karakas": karakas
        }
    except Exception as e:
        raise HTTPException(400, f"Calculation error: {e}")

# ====== Novi endpoint: /chart_place (kraj + lokalni čas) ======

tf = TimezoneFinder()

class PlaceData(BaseModel):
    place: str = Field(..., description="City and country (e.g., 'Maribor, Slovenia')")
    datetime_local: str = Field(..., description="Local datetime 'YYYY-MM-DD HH:MM'")

def geocode_place(place: str) -> Tuple[float, float]:
    """Geolokacija preko OpenStreetMap Nominatim."""
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": place, "format": "json", "limit": 1}
    r = requests.get(url, params=params, headers={"User-Agent":"MyJHoraAPI/1.0"})
    if not r.ok or not r.json():
        raise HTTPException(400, f"Cannot geocode location: {place}")
    d = r.json()[0]
    return float(d["lat"]), float(d["lon"])

@app.post("/chart_place")
def chart_place(data: PlaceData):
    """Izračun karte po lokalnem času in kraju, identično JHora (Lahiri, mean node, Placidus)."""
    try:
        lat, lon = geocode_place(data.place)
        tz_name = tf.timezone_at(lat=lat, lng=lon)
        if tz_name is None:
            raise HTTPException(400, f"Cannot find timezone for {data.place}")

        # lokalni čas -> aware datetime
        dt_local_naive = datetime.strptime(data.datetime_local, "%Y-%m-%d %H:%M")
        tzinfo = zoneinfo.ZoneInfo(tz_name)
        dt_local = dt_local_naive.replace(tzinfo=tzinfo)

        # offset v urah (pozitivno za CET=+1, CEST=+2, itd.)
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
