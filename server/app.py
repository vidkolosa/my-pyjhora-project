# server/app.py — v0.6.0 JHora-perfect Sripati + chart_place
import os, sys
from typing import Optional, Dict
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import swisseph as swe
import pytz
from timezonefinder import TimezoneFinder

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(ROOT, ".."))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

EPHE_PATH = os.path.join(SRC, "jhora", "data", "ephe")
os.makedirs(EPHE_PATH, exist_ok=True)
swe.set_ephe_path(EPHE_PATH)
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
FLAGS_SID = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio",
         "Sagittarius","Capricorn","Aquarius","Pisces"]
APP_VERSION = "0.6.0"

class BirthData(BaseModel):
    year:int; month:int; day:int
    hour:int; minute:int
    lat:float; lon:float
    tz:Optional[float]=None

app=FastAPI(title="JHora PyAPI",version=APP_VERSION)

def norm(x): return x%360
def sign_of(lon): return SIGNS[int(lon//30)%12]

def tz_offset(bd:BirthData)->float:
    if bd.tz is not None: return float(bd.tz)
    tf=TimezoneFinder()
    tzname=tf.timezone_at(lat=bd.lat,lng=bd.lon) or "Europe/Ljubljana"
    tz=pytz.timezone(tzname)
    local=tz.localize(datetime(bd.year,bd.month,bd.day,bd.hour,bd.minute))
    return local.utcoffset().total_seconds()/3600

def julday_ut(bd:BirthData)->float:
    ut=bd.hour+bd.minute/60 - tz_offset(bd)
    return swe.julday(bd.year,bd.month,bd.day,ut)

def planets_sid(jd:float)->Dict[str,float]:
    ids={"Sun":swe.SUN,"Moon":swe.MOON,"Mars":swe.MARS,"Mercury":swe.MERCURY,
         "Jupiter":swe.JUPITER,"Venus":swe.VENUS,"Saturn":swe.SATURN,"Rahu":swe.MEAN_NODE}
    out={k:norm(swe.calc_ut(jd,p,FLAGS_SID)[0][0]) for k,p in ids.items()}
    out["Ketu"]=norm(out["Rahu"]+180); return out

def sripati_houses(jd,lat,lon):
    # vzemi sidereal Asc in MC iz SwissEph
    ascmc = swe.houses_ex(jd,lat,lon,b'P',FLAGS_SID)[1]
    asc = norm(ascmc[0]); mc = norm(ascmc[1])
    desc = norm(asc+180); ic = norm(mc+180)
    # Porphyry delitev vsakega kvadranta
    quads=[asc, mc, desc, ic, asc+360]
    bhavas=[]
    for i in range(4):
        span=(quads[i+1]-quads[i])%360
        for j in range(3):
            bhavas.append(norm(quads[i] + span*(j/3)))
    return bhavas, asc

def chara_karakas(pl):
    use=["Sun","Moon","Mars","Mercury","Jupiter","Venus","Saturn","Rahu"]
    within={p:(30-(pl[p]%30) if p=="Rahu" else pl[p]%30) for p in use}
    order=["Atmakaraka","Amatyakaraka","Bhratrukaraka","Matrukaraka",
           "Pitrukaraka","Putrakaraka","Gnatikaraka","Darakaraka"]
    ranked=sorted(use,key=lambda x:(within[x],pl[x]%30),reverse=True)
    return {order[i]:{"planet":ranked[i],"sign":sign_of(pl[ranked[i]])} for i in range(8)}

@app.get("/health")
def health():
    ephe_ok = os.path.isdir(EPHE_PATH) and bool(os.listdir(EPHE_PATH))
    return {"ok":True,"version":APP_VERSION,"ephe_loaded":ephe_ok}

@app.post("/chart_full")
def chart_full(data: BirthData):
    jd = julday_ut(data)
    pl = planets_sid(jd)

    # Sripati/Porphyry – dobimo 12 bhava MADDHY (središča) in Asc
    bhava_madhya, asc = sripati_houses(jd, data.lat, data.lon)

    # PRAVILNO: meje bhav (SANDHI) so sredine med sosednjima madhyama
    sandhi = []
    for i in range(12):
        a = bhava_madhya[i]
        b = bhava_madhya[(i + 1) % 12]
        # sredina na krogu
        span = (b - a) % 360.0
        sandhi.append((a + span / 2.0) % 360.0)

    # planet -> bhava po intervalu sandhi[i] → sandhi[i+1]
    def bhava_index(lon: float) -> int:
        L = lon % 360.0
        for i in range(12):
            start = sandhi[i]
            end = sandhi[(i + 1) % 12]
            if end < start:  # wrap čez 0°
                if L >= start or L < end:
                    return i + 1
            else:
                if start <= L < end:
                    return i + 1
        return 12

    planets = {
        p: {
            "deg": round(lon, 2),
            "sign": sign_of(lon),
            "bhava": bhava_index(lon)
        }
        for p, lon in pl.items()
    }

    return {
        "ayanamsa": "Lahiri",
        "house_system": "Sripati",
        "ascendant": {"deg": round(asc, 2), "sign": sign_of(asc)},
        "bhavas": {f"Bhava{i+1}": round(b, 2) for i, b in enumerate(bhava_madhya)},
        "bhava_sandhis": [round(x, 2) for x in sandhi],
        "planets": planets,
        "chara_karakas": chara_karakas(pl)
    }


# Tvoj “place” test – demo za Maribor
@app.post("/chart_place")
def chart_place(payload:Dict):
    place = payload.get("place"); dt = payload.get("datetime_local")
    if not place or not dt: raise HTTPException(400,"Missing 'place' or 'datetime_local'")
    if "Maribor" in place:
        lat, lon = 46.55, 15.98
    else:
        raise HTTPException(400, "Place not supported in demo chart_place")
    y,m,d = map(int, dt.split(" ")[0].split("-"))
    hh,mm = map(int, dt.split(" ")[1].split(":"))
    bd = BirthData(year=y,month=m,day=d,hour=hh,minute=mm,lat=lat,lon=lon)
    return chart_full(bd)
