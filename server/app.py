# server/app.py
import os, sys
from typing import Optional, Dict
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import swisseph as swe
import pytz
from timezonefinder import TimezoneFinder

# --- Paths & Ephemeris setup ---
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]
NAKSHATRAS = [
    "Ashvini","Bharani","Krittika","Rohini","Mrigashira","Ardra","Punarvasu",
    "Pushya","Ashlesha","Magha","Purva Phalguni","Uttara Phalguni","Hasta",
    "Chitra","Swati","Vishakha","Anuradha","Jyeshtha","Mula","Purva Ashadha",
    "Uttara Ashadha","Shravana","Dhanishtha","Shatabhisha","Purva Bhadrapada",
    "Uttara Bhadrapada","Revati"
]

class BirthData(BaseModel):
    name: Optional[str] = None
    year: int; month: int; day: int
    hour: int; minute: int
    lat: float; lon: float
    tz: Optional[float] = Field(default=None, description="Offset from GMT (auto if missing)")
    house_system: str = Field(default="P")

app = FastAPI(title="JHora PyAPI", version="0.3.0")

def sign_of(lon: float): return SIGNS[int(lon // 30) % 12]
def nakshatra_of(lon):
    idx = int((lon % 360) / (360/27)); pada = int(((lon % (360/27)) / (360/108))) + 1
    return NAKSHATRAS[idx], pada

def tz_offset_hours(bd: BirthData):
    if bd.tz is not None: return float(bd.tz)
    tf = TimezoneFinder(); tzname = tf.timezone_at(lat=bd.lat, lng=bd.lon)
    if not tzname: tzname = "Europe/Ljubljana"
    tzinfo = pytz.timezone(tzname)
    local = tzinfo.localize(datetime(bd.year, bd.month, bd.day, bd.hour, bd.minute))
    return local.utcoffset().total_seconds()/3600.0

def to_julday_ut(bd: BirthData):
    tzhrs = tz_offset_hours(bd)
    ut = bd.hour + bd.minute/60 - tzhrs
    return swe.julday(bd.year, bd.month, bd.day, ut)

def planet_longitudes_sidereal(jd_ut):
    ids = {"Sun":swe.SUN,"Moon":swe.MOON,"Mars":swe.MARS,"Mercury":swe.MERCURY,
           "Jupiter":swe.JUPITER,"Venus":swe.VENUS,"Saturn":swe.SATURN,"Rahu":swe.MEAN_NODE}
    out = {n: swe.calc_ut(jd_ut,p,FLAGS)[0][0]%360 for n,p in ids.items()}
    out["Ketu"] = (out["Rahu"] + 180)%360
    return out

def ascendant_sidereal(jd_ut, lat, lon, hs):
    houses, ascmc = swe.houses_ex(jd_ut, lat, lon, hs.encode(), swe.FLG_SIDEREAL)
    return ascmc[0]%360

def chara_karakas_jhora(pl):
    use = ["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]
    within={p:(30-(pl[p]%30) if p=="Rahu" else pl[p]%30) for p in use}
    order=["Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
           "Pitrukaraka","Putrakaraka","Gnatikaraka","Darakaraka"]
    ranked=sorted(use,key=lambda x:(within[x],pl[x]%30),reverse=True)
    return {order[i]:{"planet":ranked[i],"sign":sign_of(pl[ranked[i]]),"deg":round(pl[ranked[i]],2)} for i in range(8)}

@app.get("/health")
def health():
    return {"ok":True,"ephe":EPHE_PATH,"exists":os.path.isdir(EPHE_PATH)}

@app.post("/chart")
def chart(data: BirthData):
    try:
        jd = to_julday_ut(data)
        pl = planet_longitudes_sidereal(jd)
        asc = ascendant_sidereal(jd,data.lat,data.lon,data.house_system)
        moon_ns,moon_pada = nakshatra_of(pl["Moon"])
        ck = chara_karakas_jhora(pl)
        return {
            "ayanamsa":"Lahiri","node":"Mean","house_system":data.house_system,
            "ascendant":{"deg":round(asc,2),"sign":sign_of(asc)},
            "planets":{k:{"deg":round(v,2),"sign":sign_of(v)} for k,v in pl.items()},
            "moon_nakshatra":{"name":moon_ns,"pada":moon_pada},
            "chara_karakas":ck
        }
    except Exception as e:
        raise HTTPException(400, str(e))
