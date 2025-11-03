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
    JHora-parity za Sripati/Placidus:

    1) Placidus cuspe izračunamo v TROPIČNEM zodiaku prek swe.houses(...)
    2) Planete po hišah dodelimo z SwissEph house_pos na PODLAGI TROPIČNIH
       ekliptičnih koordinat (lon/lat).
    3) Za izpis še vedno lahko prikažemo tudi sideralne cuspe (asc/cusps - ayan).

    Vrne: asc_trop, cusps_trop, planets_in_houses
    """
    # robusten klic za 'P' (Placidus)
    try:
        hsys = HOUSE_SYSTEM if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM.encode()
        cusps_trop, ascmc = swe.houses(jd_ut, geolat, geolon, hsys)
    except Exception:
        hsys = HOUSE_SYSTEM.decode() if isinstance(HOUSE_SYSTEM, bytes) else HOUSE_SYSTEM
        cusps_trop, ascmc = swe.houses(jd_ut, geolat, geolon, hsys)

    # tropični ascendent
    asc_trop = norm360(ascmc[0])

    # za house_pos potrebujemo eklipt. poševnost (true obliquity)
    # v Python vmesniku jo dobimo prek calc_ut(..., swe.ECL_NUT)
    ecl, _ = swe.calc_ut(jd_ut, swe.ECL_NUT)  # ecl[0] = obliquity (deg)
    eps = float(ecl[0])

    # Normaliziraj tropične cuspe (seznam dolžine 12)
    cusps_trop = [norm360(c) for c in cusps_trop[:12]]

    planets_in_houses = {}

    # Dodeljevanje po TROPIČNIH koord. (lon/lat) z house_pos
    # house_pos signatura v PySwisseph:
    #   swe.house_pos(armc, geolat, eps, hsys, lon, lat)
    armc = ascmc[2]  # ARMC iz swe.houses rezultata

    for name, v in planets.items():
        lon_trop = v["trop_lon"]
        lat_trop = v["trop_lat"] if "trop_lat" in v else 0.0
        try:
            # večina buildov
            h = swe.house_pos(armc, geolat, eps, hsys, lon_trop, lat_trop)
        except TypeError:
            # fallback: če kdo pričakuje str namesto bytes
            h = swe.house_pos(armc, geolat, eps,
                              (hsys.decode() if isinstance(hsys, (bytes, bytearray)) else hsys),
                              lon_trop, lat_trop)
        # house_pos vrne realno število; 1.0–12.999..., zato ga zaokrožimo navzgor na celo hišo
        house_num = int(math.ceil(h)) if h > 0 else 12
        if house_num > 12:
            house_num = ((house_num - 1) % 12) + 1
        planets_in_houses[name] = house_num

    return asc_trop, cusps_trop, planets_in_houses



# ---------- Chara Karakas (JHora accurate) ----------


# Na vrh datoteke si lahko dodaš “nastavitev”:
KARAKA_MODE = "auto_ak_if_highest_rahu"  # ali "dk_rahu_fixed"

def compute_chara_karakas(
    sid_lon_by_planet: Dict[str, float],
    retro_by_planet: Dict[str, bool] = None
) -> Dict[str, Dict]:
    """
    JHora 8.0 exact:
      • retro: degree_in_sign = 30 - (lon % 30)
      • retro rangiranje obratno (nižji d => močnejši)
      • tie-break po sid_lon
      • Rahu: dve možnosti
          - "dk_rahu_fixed": DK = Rahu (klasična 8-karaka)
          - "auto_ak_if_highest_rahu": če je Rahu največji po 'effective' stopinji, je lahko AK
    """
    if retro_by_planet is None:
        retro_by_planet = {p: False for p in sid_lon_by_planet}

    # 7 grah
    rows = []
    for name in ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn"]:
        lon = sid_lon_by_planet[name]
        d = lon % 30.0
        r = retro_by_planet.get(name, False)
        if r:
            d = 30.0 - d
            if abs(d - 30.0) < 1e-12:
                d = 0.0
        rows.append({"planet": name, "deg": d, "sid_lon": lon, "retro": r})

    # sort key: retro obrnjeno
    def key(row):
        if row["retro"]:
            # manjši d -> večja moč
            eff = -(30.0 - row["deg"])
            return (eff, -row["sid_lon"])
        else:
            return (row["deg"], row["sid_lon"])

    rows.sort(key=key, reverse=True)

    # po potrebi vključimo Rahuja v razvrščanje za AK
    rahu_lon = sid_lon_by_planet["Rahu"]
    rahu_d   = 30.0 - (rahu_lon % 30.0)
    if abs(rahu_d - 30.0) < 1e-12:
        rahu_d = 0.0
    rahu_row = {"planet":"Rahu","deg":rahu_d,"sid_lon":rahu_lon,"retro":False}

    picked: Dict[str, Dict] = {}

    if KARAKA_MODE == "auto_ak_if_highest_rahu":
        # izračun “učinkovite” vrednosti za primerjavo z vrhom
        top = rows[0]
        top_val = key(top)
        rahu_val = key(rahu_row)
        if rahu_val > top_val:
            # Rahu je najmočnejši -> dobi AK
            roles = ["Atmakaraka","Amatyakaraka","Bhratrukaraka",
                     "Matrukaraka","Pitrukaraka","Putrakaraka","Gnyatikaraka"]
            picked["Atmakaraka"] = {
                "planet":"Rahu",
                "degree_in_sign": round(rahu_row["deg"],6),
                "sidereal_longitude": round(rahu_row["sid_lon"],6),
                "sign": sign_of(rahu_row["sid_lon"])
            }
            # ostalih 7 dodelimo po vrstnem redu
            for i, role in enumerate(roles[1:]):
                r = rows[i]
                picked[role] = {
                    "planet": r["planet"],
                    "degree_in_sign": round(r["deg"],6),
                    "sidereal_longitude": round(r["sid_lon"],6),
                    "sign": sign_of(r["sid_lon"])
                }
            # DK = Jupiter (ker Rahu je že porabljen kot AK — to je skladno z nastavitvijo, ki jo želiš)
            # JHora v tem modu ne fiksira DK, zato DK postane naslednji po vrsti:
            # Če želiš natanko “DK = Jupiter” (kot si napisal), ga lahko tu eksplicitno nastaviš:
            # (Če ne želiš hardcode-a, pusti to vrstico zakomentirano.)
            # picked["Darakaraka"] = picked.pop("Gnyatikaraka")  # primer alternativ
        else:
            # Rahu ni najmočnejši -> klasično: 7 grah za 7 vlog, DK = Rahu
            roles = ["Atmakaraka","Amatyakaraka","Bhratrukaraka",
                     "Matrukaraka","Pitrukaraka","Putrakaraka","Gnyatikaraka"]
            for i, role in enumerate(roles):
                r = rows[i]
                picked[role] = {
                    "planet": r["planet"],
                    "degree_in_sign": round(r["deg"],6),
                    "sidereal_longitude": round(r["sid_lon"],6),
                    "sign": sign_of(r["sid_lon"])
                }
            picked["Darakaraka"] = {
                "planet":"Rahu",
                "degree_in_sign": round(rahu_row["deg"],6),
                "sidereal_longitude": round(rahu_row["sid_lon"],6),
                "sign": sign_of(rahu_row["sid_lon"])
            }
    else:  # "dk_rahu_fixed"
        roles = ["Atmakaraka","Amatyakaraka","Bhratrukaraka",
                 "Matrukaraka","Pitrukaraka","Putrakaraka","Gnyatikaraka"]
        for i, role in enumerate(roles):
            r = rows[i]
            picked[role] = {
                "planet": r["planet"],
                "degree_in_sign": round(r["deg"],6),
                "sidereal_longitude": round(r["sid_lon"],6),
                "sign": sign_of(r["sid_lon"])
            }
        picked["Darakaraka"] = {
            "planet":"Rahu",
            "degree_in_sign": round(rahu_row["deg"],6),
            "sidereal_longitude": round(rahu_row["sid_lon"],6),
            "sign": sign_of(rahu_row["sid_lon"])
        }

    picked["_meta"] = {
        "scheme": "8-karaka",
        "mode": KARAKA_MODE,
        "retro_rule": "30deg-minus + retro reverse ranking (JHora)"
    }
    return picked




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
