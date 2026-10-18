"""География: расстояния, реальные маршруты (пешком / на машине), погода, поиск точки по названию.

Все внешние сервисы бесплатные и без ключей:
- пешие и автомобильные маршруты — OSRM на серверах FOSSGIS (routing.openstreetmap.de)
- погода — Open-Meteo
Если сервис недоступен, считаем по прямой с поправкой и честно помечаем «оценка».
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "okno-astana-hackathon/0.1 (github.com/rsduv/okno-astana)"}
WALK_KMH = 4.5
DETOUR = 1.35         # улицы не идут по прямой: реальный путь ≈ прямая × 1.3 (замер в Астане: 1.07–1.35)
TAXI_WAIT_MIN = 6     # подача такси — оценка
_client = httpx.Client(timeout=12, headers=UA)
_route_client = httpx.Client(timeout=5, headers=UA)
CACHE_FILE = ROOT / "data" / "route_cache.json"
try:
    _route_cache: dict[str, dict] = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
except Exception:
    _route_cache = {}
_breaker = {"until": 0.0}  # если сервис маршрутов не отвечает — минуту не дёргаем его, считаем по оценке
_weather_cache: dict = {}


def haversine_km(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def walk_min_est(a, b) -> float:
    return haversine_km(a, b) * DETOUR / WALK_KMH * 60


def traffic_factor(t: datetime) -> tuple[float, str]:
    """Поправка на пробки по часу и дню. Живых данных о пробках бесплатно нет — это честная оценка
    по типичным часам пик Астаны (утро 8–10, вечер 17–19:30 в будни)."""
    h = t.hour + t.minute / 60
    weekday = t.weekday() < 5
    if weekday and 7.5 <= h < 10:
        return 1.7, "утренний час пик"
    if weekday and 17 <= h < 19.5:
        return 1.9, "вечерний час пик"
    if 22 <= h or h < 6:
        return 1.0, "ночь, дороги свободны"
    return 1.3, "обычный трафик"


def car_min_est(a, b, t: datetime) -> float:
    f, _ = traffic_factor(t)
    return haversine_km(a, b) * 1.4 / 32 * 60 * f


def route(a, b, mode: str, fallback: bool = False) -> dict | None:
    """Реальный маршрут по улицам. mode: 'foot' | 'car'. Возвращает {km, min, coords} или None.
    fallback=True: если OSRM недоступен — спросить 2GIS (только для финального плана, чтобы беречь лимит)."""
    key = f"{mode}:{a[0]:.5f},{a[1]:.5f}:{b[0]:.5f},{b[1]:.5f}"
    if key in _route_cache:
        return _route_cache[key]
    res = _osrm(a, b, mode, key)
    if res is None and fallback:
        from . import dgis
        res = dgis.path(a, b, mode)
        if res and mode == "foot":
            res = {**res, "min": res["km"] / WALK_KMH * 60}
    return res


def _osrm(a, b, mode, key):
    if time.time() < _breaker["until"]:
        return None
    prof = "routed-foot" if mode == "foot" else "routed-car"
    url = (f"https://routing.openstreetmap.de/{prof}/route/v1/driving/"
           f"{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}?overview=full&geometries=geojson")
    try:
        r = _route_client.get(url)
        r.raise_for_status()
        rt = r.json()["routes"][0]
        coords = [[round(x, 5), round(y, 5)] for x, y in rt["geometry"]["coordinates"]]
        res = {"km": rt["distance"] / 1000, "min": rt["duration"] / 60, "coords": coords}
        if mode == "foot":  # сервер считает ~5 км/ч; приводим к спокойному шагу 4.5 км/ч
            res["min"] = res["km"] / WALK_KMH * 60
    except Exception:
        _breaker["until"] = time.time() + 60
        return None
    _route_cache[key] = res
    return res


def save_cache():
    try:
        CACHE_FILE.write_text(json.dumps(_route_cache), encoding="utf-8")
    except Exception:
        pass


def weather(override: dict | None = None) -> dict:
    """Погода в Астане сейчас. override = {'apparent': -25, 'wind': 30} для демо-симуляции."""
    if override:
        t = float(override.get("apparent", -25))
        return {"temp": t + 5, "apparent": t, "wind": float(override.get("wind", 30)), "simulated": True,
                "precip": 0.0, "is_day": True, "sunset": None, "sky": "snow", "aqi": None, "source": "симуляция для демо"}
    if _weather_cache and time.time() - _weather_cache["_ts"] < 600:
        return _weather_cache["data"]
    try:
        r = _client.get("https://api.open-meteo.com/v1/forecast", timeout=5, params={
            "latitude": 51.13, "longitude": 71.43, "timezone": "Asia/Almaty",
            "current": "temperature_2m,apparent_temperature,wind_speed_10m,precipitation,is_day,weather_code,snowfall,rain",
            "daily": "sunset,sunrise", "forecast_days": 1})
        d = r.json()
        c = d["current"]
        data = {"temp": c["temperature_2m"], "apparent": c["apparent_temperature"], "wind": c["wind_speed_10m"],
                "precip": c.get("precipitation", 0), "is_day": bool(c.get("is_day", 1)),
                "sunset": d["daily"]["sunset"][0][-5:], "sunrise": d["daily"]["sunrise"][0][-5:],
                "simulated": False, "source": "Open-Meteo, сейчас",
                "sky": _sky(c), "code": c.get("weather_code")}
        data["aqi"] = air()
    except Exception:
        data = {"temp": None, "apparent": None, "wind": None, "precip": 0, "is_day": True, "sunset": None,
                "simulated": False, "source": "погода недоступна"}
    _weather_cache.update({"_ts": time.time(), "data": data})
    return data


def _sky(c: dict) -> str | None:
    """Осадки для фона интерфейса: 'snow' | 'rain' | None (коды WMO)."""
    code = c.get("weather_code") or 0
    if (c.get("snowfall") or 0) > 0 or code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if (c.get("rain") or 0) > 0 or code in (51, 53, 55, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99):
        return "rain"
    return None


def air() -> int | None:
    """Качество воздуха (US AQI) — зимой в Астане бывает смог от ТЭЦ и печного отопления. Open-Meteo, без ключа."""
    try:
        r = _client.get("https://air-quality-api.open-meteo.com/v1/air-quality", timeout=4, params={
            "latitude": 51.13, "longitude": 71.43, "current": "us_aqi", "timezone": "Asia/Almaty"})
        return r.json()["current"]["us_aqi"]
    except Exception:
        return None


def cold_level(w: dict) -> str:
    """'ok' | 'cold' | 'severe' — сколько можно идти по улице подряд."""
    a = w.get("apparent")
    if a is None:
        return "ok"
    if a <= -15 or (w.get("wind") or 0) >= 45:
        return "severe"
    if a <= -7 or (w.get("precip") or 0) > 0.5 or (w.get("wind") or 0) >= 30 or (w.get("aqi") or 0) > 150:
        return "cold"
    return "ok"


MAX_OUTDOOR_LEG = {"ok": 35, "cold": 22, "severe": 12}   # минут подряд на улице


# ---------- поиск точки по названию ----------
LANDMARKS = json.loads((ROOT / "data" / "landmarks.json").read_text(encoding="utf-8"))


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-яёәіңғүұқөһ0-9 ]", " ", s.lower()).strip()


def _fuzzy_landmark(q: str):
    """Опечатки: «байтерика» ≈ «байтерек», «хаан шатыр» ≈ «хан шатыр». Только для слов от 5 букв и похожести ≥ 0,8."""
    from difflib import SequenceMatcher
    toks = q.split()
    best = None
    for lm in LANDMARKS:
        for al in lm["aliases"]:
            a = _norm(al)
            if len(a) < 5:
                continue
            for i in range(len(toks)):
                for n in (1, 2):  # одно или два слова подряд («хаан шатыра»)
                    chunk = " ".join(toks[i:i + n])
                    if len(chunk) < 5:
                        continue
                    r = SequenceMatcher(None, chunk, a).ratio()
                    if r >= 0.8 and (not best or r > best[0]):
                        best = (r, lm)
    return (len(best[1]["name"]), best[1]) if best else None


def reverse(lat: float, lon: float, lang: str = "ru") -> str | None:
    """Адрес «улица, дом» по точке — для плашки «я здесь». Nominatim (OpenStreetMap), без ключа; ничего не сохраняем."""
    try:
        r = _client.get("https://nominatim.openstreetmap.org/reverse", timeout=5, params={
            "lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18, "addressdetails": 1,
            "accept-language": {"kk": "kk,ru", "en": "en,ru"}.get(lang, "ru")})
        a = r.json().get("address", {})
    except Exception:
        return None
    street = a.get("road") or a.get("pedestrian") or a.get("footway") or a.get("residential")
    if street:
        return street + (f", {a['house_number']}" if a.get("house_number") else "")
    return a.get("neighbourhood") or a.get("suburb") or a.get("city_district")


def find_place(text: str, pois: list[dict] | None = None, saved: dict | None = None) -> dict | None:
    """Ищет место по названию: свои места пользователя («дом», «тётя Гуля») → справочник ориентиров → база POI →
    поиск 2GIS (понимает сокращения: «мну универ») → геокодер OSM."""
    if not text:
        return None
    q = _norm(text)
    qw = q.replace("ё", "е").split()
    for name, pt in (saved or {}).items():
        nw = _norm(name).replace("ё", "е").split()
        # «к тёте Гуле» = «Тётя Гуля»: каждое слово названия совпадает с каким-то словом запроса по основе
        stem = lambda w: w[:max(3, len(w) - 2)]
        if nw and all(any(x.startswith(stem(w)) or w.startswith(stem(x)) for x in qw) for w in nw):
            return {"lat": round(pt["lat"], 3), "lon": round(pt["lon"], 3), "label": name, "source": "твоё сохранённое место"}
    best = None
    for lm in LANDMARKS:
        for al in lm["aliases"]:
            if re.search(rf"(^| ){re.escape(_norm(al))}( |$)", q) or q == _norm(al):
                if not best or len(al) > best[0]:
                    best = (len(al), lm)
    if not best:
        best = _fuzzy_landmark(q)
    if best:
        lm = best[1]
        return {"lat": lm["lat"], "lon": lm["lon"], "label": lm["name"], "source": "справочник ориентиров"}
    if pois:
        for p in pois:
            n = _norm(p["name"])
            if len(n) >= 4 and (n == q or (len(q) >= 5 and q in n) or (len(n) >= 6 and n in q)):
                return {"lat": p["lat"], "lon": p["lon"], "label": p["name"], "source": "база мест OSM"}
    from . import dgis
    g = dgis.geocode(text)
    if g:
        return g
    try:
        r = _client.get("https://nominatim.openstreetmap.org/search", timeout=6, params={
            "q": text + ", Астана", "format": "jsonv2", "limit": 1,
            "viewbox": "71.2,51.28,71.65,51.0", "bounded": 1})
        d = r.json()
        if d:
            return {"lat": float(d[0]["lat"]), "lon": float(d[0]["lon"]),
                    "label": d[0]["name"] or text, "source": "геокодер OpenStreetMap"}
    except Exception:
        pass
    return None


def round_point(lat: float, lon: float) -> tuple[float, float]:
    """Приватность: округляем точку пользователя до ~150 м и дальше работаем только с ней."""
    return round(lat, 3), round(lon, 3) if lon is not None else lon
