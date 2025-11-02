import os, sys
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# --- omogoči import "jhora" iz mape src/ ---
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    import jhora  # paket v src/
    import swisseph as swe  # pyswisseph - pravilni modul
except Exception as e:
    raise RuntimeError(f"Cannot import jhora: {e}")


# EPHE pot (napolni jo install_ephe.sh na Renderju)
EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)

app = FastAPI(title="My PyJHora API", version="0.1.0")

class BirthData(BaseModel):
    name: Optional[str] = None
    year: int; month: int; day: int
    hour: int; minute: int
    lat: float = Field(..., description="decimal degrees, N+ S-")
    lon: float = Field(..., description="decimal degrees, E+ W-")
    tz:  float = Field(..., description="timezone hours (CET=+1, CEST=+2)")

def _julday(y,m,d,h,mi,tz):
    ut = h + mi/60 - tz
    return swe.julday(y, m, d, ut)

@app.get("/health")
def health():
    ok = os.path.isdir(EPHE_PATH) and len(os.listdir(EPHE_PATH)) > 0
    return {"status":"ok", "ephe_path": EPHE_PATH, "ephe_loaded": ok}

@app.post("/chart")
def chart(data: BirthData):
    try:
        jd = _julday(data.year, data.month, data.day, data.hour, data.minute, data.tz)
        sun = swe.calc_ut(jd, swe.SUN)[0][0]
        moon = swe.calc_ut(jd, swe.MOON)[0][0]
        houses, ascmc = swe.houses_ex(jd, data.lat, data.lon, b'P')  # Placidus
        asc = ascmc[0]
        def sign_of(lon):
            signs = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
                     "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
            return signs[int(lon//30)%12]
        return {
            "name": data.name,
            "julian_day": jd,
            "sun":  {"longitude": round(sun,3),  "sign": sign_of(sun)},
            "moon": {"longitude": round(moon,3), "sign": sign_of(moon)},
            "ascendant": {"degree": round(asc,3), "sign": sign_of(asc)},
        }
    except Exception as e:
        raise HTTPException(400, f"Calculation error: {e}")
