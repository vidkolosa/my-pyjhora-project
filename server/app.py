import os, sys, math
from typing import Optional, Dict, Tuple
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# --- omogoči import "jhora" iz mape src/ ---
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    import jhora  # tvoja koda v src/
    import swisseph as swe  # pyswisseph
except Exception as e:
    raise RuntimeError(f"Cannot import jhora: {e}")

EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

app = FastAPI(title="My PyJHora API", version="0.2.0")

# ---------- Pomožne funkcije ----------
SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

def norm360(x: float) -> float:
    return x % 360.0

def sign_of(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def deg_in_sign(lon: float) -> float:
    """0..30 notranja stopinja v znamenju (za čara karake)."""
    return (lon % 30.0)

def lahiri_ayanamsa_ut(jd_ut: float) -> float:
    """Lahiri ayanamsha v stopinjah (UT)."""
    try:
        return float(swe.get_ayanamsa_ut(jd_ut))
    except Exception:
        # stare verzije pyswisseph:
        return float(swe.get_ayanamsa(jd_ut))

def to_sidereal(lon_tropical: float, ayan: float) -> float:
    return norm360(lon_tropical - ayan)

def planet_longitudes(jd_ut: float) -> Dict[str, Tuple[float,float]]:
    """
    Vrne {ime: (tropical_lon, sidereal_lon)} za 9 grah.
    Uporablja Lahiri ayanamsha in MEAN node (JHora privzeto).
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
        "Rahu":    swe.MEAN_NODE,
        "Ketu":    swe.MEAN_NODE,  # Ketu = Rahu + 180
    }

    res = {}
    for name, code in bodies.items():
        if name == "Ketu":
            # izračunaj iz Rahuja
            rahu_trop = res["Rahu"][0]
            ketu_trop = norm360(rahu_trop + 180.0)
            ketu_sid = to_sidereal(ketu_trop, ayan)
            res["Ketu"] = (ketu_trop, ketu_sid)
            continue

        lon_trop = swe.calc_ut(jd_ut, code)[0][0]
        lon_sid  = to_sidereal(lon_trop, ayan)
        res[name] = (norm360(lon_trop), norm360(lon_sid))

    return res

def compute_chara_karakas(sidereal: Dict[str, float]) -> Dict[str, Dict]:
    """
    7-karaka shema: AK, AmK, BK, MK, PK, GK, DK
    Pravila:
      - Ketu je IZKLJUČEN
      - Rahu dobi "karaka degree" = 30° - (lon % 30°) (retro pravilo)
      - razvrščanje po degree-in-sign (največji = Atmakaraka)
    """
    # pripravi seznam (planet, degree_for_ranking, sidereal_lon)
    ranking = []
    for name, lon in sidereal.items():
        if name == "Ketu":
            continue  # nikoli ni karaka
        d = deg_in_sign(lon)
        if name == "Rahu":
            d = 30.0 - d
            if abs(d - 30.0) < 1e-8:
                d = 0.0
        ranking.append((name, d, lon))

    # sort po d (desc), nato po lon (stabilno)
    ranking.sort(key=lambda x: (x[1], x[2]), reverse=True)

    order = ["Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
             "Putrakaraka","Gnyatikaraka","Darakaraka"]

    karakas = {}
    for i, role in enumerate(order):
        if i < len(ranking):
            name, d, lon = ranking[i]
            karakas[role] = {
                "planet": name,
                "degree_in_sign": round(d, 4),
                "sidereal_longitude": round(lon, 4),
                "sign": sign_of(lon),
            }
    return karakas


# ---------- API modeli ----------
class BirthData(BaseModel):
    name: Optional[str] = None
    year: int; month: int; day: int
    hour: int; minute: int
    lat: float = Field(..., description="decimal degrees, N+ S-")
    lon: float = Field(..., description="decimal degrees, E+ W-")
    tz:  float = Field(..., description="timezone hours (CET=+1, CEST=+2)")

def julday_ut(y,m,d,h,mi,tz) -> float:
    ut = h + mi/60.0 - tz
    return swe.julday(y, m, d, ut)

@app.get("/health")
def health():
    ok = os.path.isdir(EPHE_PATH) and len(os.listdir(EPHE_PATH)) > 0
    return {"status":"ok", "ephe_path": EPHE_PATH, "ephe_loaded": ok}

@app.post("/chart")
def chart(data: BirthData):
    """
    Vrne:
      - tropical & sidereal (Lahiri) longitudes za 9 grah
      - znamenja
      - ascendent
      - čara karake (7-karaka shema)
    """
    try:
        jd = julday_ut(data.year, data.month, data.day, data.hour, data.minute, data.tz)

        # planeti
        longs = planet_longitudes(jd)  # {name: (trop, sid)}
        planets = {}
        sid_only = {}
        for name, (trop, sid) in longs.items():
            planets[name] = {
                "tropical": {"longitude": round(trop, 6), "sign": sign_of(trop)},
                "sidereal": {"longitude": round(sid, 6),  "sign": sign_of(sid)},
            }
            sid_only[name] = sid

        # Asc (Placidus; Asc je vedno isti ne glede na hišni sistem)
        houses, ascmc = swe.houses_ex(jd, data.lat, data.lon, b'P')
        asc = ascmc[0]

        # čara karake (na osnovi SIDEREAL longitudes)
        karakas = compute_chara_karakas(sid_only)

        return {
            "name": data.name,
            "julian_day_ut": jd,
            "ayanamsa": round(lahiri_ayanamsa_ut(jd), 6),
            "ascendant": {"degree": round(asc, 6), "sign": sign_of(asc)},
            "planets": planets,
            "chara_karakas": karakas,
            "notes": {
                "ayanamsa": "Lahiri",
                "node": "Mean node",
                "chara_karaka_scheme": "7-karaka (AK, AmK, BK, MK, PK, GK, DK); Rahu uses (30° - degree_in_sign), Ketu excluded."
            }
        }
    except Exception as e:
        raise HTTPException(400, f"Calculation error: {e}")
