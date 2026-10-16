"""2GIS Places API — свежие часы работы, рейтинг и полезные отметки («можно с ноутбуком», «детский уголок»).

OSM остаётся основой (открытые данные, сбор автоматизирован). 2GIS только уточняет места, которые
планировщик уже выбрал как кандидатов: так запросов мало (демо-ключ ограничен), а данные свежие.
Ответы кэшируются локально в data/dgis_cache.json — этот файл не публикуется (данные 2GIS не наши).
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("DGIS_KEY", "")
CACHE = Path(__file__).resolve().parent.parent / "data" / "dgis_cache.json"
BUDGET = int(os.getenv("DGIS_BUDGET", "800"))  # демо-ключ: ~1000 запросов на сервис, оставляем запас
URL = "https://catalog.api.2gis.com/3.0/items"
FIELDS = "items.point,items.schedule,items.reviews,items.attribute_groups"
SKIP = {"парк", "сад", "памятник", "фонтан", "смотровая", "детская площадка", "достопримечательность"}
USEFUL = re.compile(r"ноутбук|wi-?fi|розетк|детск|английск|чек|завтрак|круглосуточ|тихо|коворк|веган|халял", re.I)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OSM_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

_lock = threading.Lock()
try:
    _cache = json.loads(CACHE.read_text(encoding="utf-8"))
except Exception:
    _cache = {"_count": 0}


_dead = {"until": 0.0}  # 2GIS ответил «лимит/ключ не работает» — пауза, живём на OSM
# Демо-ключ: Routing API — не больше 5 запросов в минуту (письмо 2GIS от 16.10). Держим запас.
PER_MIN = {"route": int(os.getenv("DGIS_ROUTE_PER_MIN", "4")), "places": int(os.getenv("DGIS_PLACES_PER_MIN", "20"))}
_stamps = {"route": [], "places": []}


def _slot(service: str) -> bool:
    """Есть ли свободный запрос в этой минуте. Нет — не ждём, а обходимся своей оценкой (ответ остаётся быстрым)."""
    with _lock:
        now = time.time()
        st = [t for t in _stamps[service] if now - t < 60]
        if len(st) >= PER_MIN[service]:
            _stamps[service] = st
            return False
        st.append(now)
        _stamps[service] = st
        return True


def _alive() -> bool:
    return bool(KEY) and os.getenv("DGIS_OFF") != "1" and time.time() > _dead["until"]


def _check(resp) -> dict | None:
    """Разбор ответа 2GIS; при исчерпанном лимите/плохом ключе — выключаемся на час, приложение работает дальше."""
    try:
        d = resp.json()
    except Exception:
        d = {}
    code = (d.get("meta") or {}).get("code", resp.status_code)
    if resp.status_code == 429 or code == 429:
        _dead["until"] = time.time() + 65  # поминутный лимит — через минуту снова можно
        return None
    if resp.status_code in (401, 402, 403) or code in (401, 402, 403):
        _dead["until"] = time.time() + 3600
        return None
    return d


def enabled() -> bool:
    return _alive() and _cache.get("_count", 0) < BUDGET


def save():
    with _lock:
        CACHE.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]", "", s.lower().split(",")[0])


def _dist_m(a, b) -> float:
    dy = (a[0] - b[0]) * 111_000
    dx = (a[1] - b[1]) * 111_000 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def _to_osm(schedule: dict) -> str | None:
    """{"Mon": {"working_hours": [{"from": "09:00", "to": "24:00"}]}} -> "Mo 09:00-24:00; …" для нашего парсера часов."""
    if not schedule:
        return None
    if schedule.get("is_24x7"):
        return "24/7"
    parts = []
    for d, od in zip(DAYS, OSM_DAYS):
        hrs = (schedule.get(d) or {}).get("working_hours") or []
        spans = ",".join(f"{h['from']}-{h['to']}" for h in hrs if h.get("from") and h.get("to"))
        parts.append(f"{od} {spans or 'off'}")
    return "; ".join(parts)


def _pick(items, p):
    """Тот же ли это объект: близко (≤120 м) и похожее название, либо совсем рядом и единственный."""
    name = _norm(p["name"])
    best = None
    for it in items:
        pt = it.get("point") or {}
        if "lat" not in pt:
            continue
        d = _dist_m((p["lat"], p["lon"]), (pt["lat"], pt["lon"]))
        n = _norm(it.get("name", ""))
        similar = bool(name and n) and (name in n or n in name or name[:5] == n[:5])
        if d <= 120 and similar:
            return it
        if d <= 40 and best is None:
            best = it
    return best if len(items) == 1 else None


def _fetch(p: dict) -> dict | None:
    if not _slot("places"):
        return None
    with _lock:
        _cache["_count"] = _cache.get("_count", 0) + 1
    try:
        r = _check(httpx.get(URL, timeout=6, params={"q": p["name"], "point": f"{p['lon']},{p['lat']}", "radius": 200,
                                                     "page_size": 3, "fields": FIELDS, "locale": "ru_KZ", "key": KEY}))
        if r is None:
            return None
        items = r.get("result", {}).get("items", [])
    except Exception:
        return None
    it = _pick(items, p)
    if not it:
        return {"match": False}
    rv = it.get("reviews") or {}
    attrs = [a.get("name", "") for g in it.get("attribute_groups") or [] for a in g.get("attributes") or []]
    return {"match": True, "id": it.get("id"), "name": it.get("name"),
            "rating": rv.get("general_rating"), "reviews": rv.get("general_review_count_with_stars") or rv.get("general_review_count"),
            "hours": _to_osm(it.get("schedule")), "attrs": [a.replace(" ", " ") for a in attrs if USEFUL.search(a)][:8]}


def info(p: dict) -> dict | None:
    """Данные 2GIS для места OSM (из кэша или одним запросом). None — 2GIS недоступен или не нашёл."""
    if p["category"] in SKIP or not KEY:
        return None
    key = p["id"]
    if key not in _cache:
        if not enabled():
            return None
        d = _fetch(p)
        if d is None:
            return None
        with _lock:
            _cache[key] = d
    d = _cache[key]
    return d if d.get("match") else None


def apply(p: dict) -> None:
    """Подмешать 2GIS к месту: свежие часы (приоритет над OSM), рейтинг, отметки."""
    if "dgis" in p:
        return
    d = info(p)
    p["dgis"] = d
    if d and d.get("hours"):
        p.setdefault("osm_hours", p.get("tags", {}).get("opening_hours"))
        p["tags"] = {**p.get("tags", {}), "opening_hours": d["hours"]}
        p["hours_src"] = "2GIS"


# ---------- пробки: 2GIS Routing API (время на машине с учётом пробок) ----------
ROUTE_URL = "https://routing.api.2gis.com/routing/7.0.0/global"
ROUTE_BUDGET = int(os.getenv("DGIS_ROUTE_BUDGET", "600"))


def drive(a, b, t, now_real) -> tuple[float, str] | None:
    """Минуты на машине из a в b при выезде в t. Если t ≈ сейчас — живые пробки, иначе — типичные для этого часа.
    Возвращает (минуты, подпись) или None (нет ключа / лимит / сбой — тогда остаётся наша оценка)."""
    if not _alive():
        return None
    live = abs((t - now_real).total_seconds()) < 20 * 60
    bucket = "live" if live else f"{t.weekday()}-{t.hour}"
    key = f"r:{a[0]:.3f},{a[1]:.3f}>{b[0]:.3f},{b[1]:.3f}@{bucket}"
    if key in _cache and (not live or _cache[key].get("ts", 0) > time.time() - 600):
        d = _cache[key]
    else:
        if _cache.get("_route_count", 0) >= ROUTE_BUDGET or not _slot("route"):
            return None
        body = {"points": [{"type": "stop", "lon": a[1], "lat": a[0]}, {"type": "stop", "lon": b[1], "lat": b[0]}],
                "transport": "driving", "route_mode": "fastest", "output": "summary",
                "traffic_mode": "jam" if live else "statistics"}
        if not live:
            # время выезда по Астане (UTC+5) -> unix time
            body["utc"] = int((t - timedelta(hours=5)).replace(tzinfo=timezone.utc).timestamp())
        with _lock:
            _cache["_route_count"] = _cache.get("_route_count", 0) + 1
        try:
            r = _check(httpx.post(ROUTE_URL, params={"key": KEY}, json=body, timeout=6))
            d = {"min": r["result"][0]["duration"] / 60, "ts": time.time()}
        except Exception:
            return None
        with _lock:
            _cache[key] = d
    return d["min"], ("пробки 2GIS сейчас" if live else f"пробки 2GIS: обычно в {t:%H}:00 в этот день недели")


PATH_BUDGET = int(os.getenv("DGIS_PATH_BUDGET", "400"))


def path(a, b, mode: str) -> dict | None:
    """Запасной маршрут по улицам от 2GIS (если OSRM недоступен): {km, min, coords}. mode: 'foot' | 'car'."""
    if not _alive() or _cache.get("_path_count", 0) >= PATH_BUDGET:
        return None
    key = f"p:{mode}:{a[0]:.4f},{a[1]:.4f}>{b[0]:.4f},{b[1]:.4f}"
    if key in _cache:
        return _cache[key]
    if not _slot("route"):
        return None
    with _lock:
        _cache["_path_count"] = _cache.get("_path_count", 0) + 1
    body = {"points": [{"type": "stop", "lon": a[1], "lat": a[0]}, {"type": "stop", "lon": b[1], "lat": b[0]}],
            "transport": "walking" if mode == "foot" else "driving", "output": "detailed"}
    try:
        r = _check(httpx.post(ROUTE_URL, params={"key": KEY}, json=body, timeout=6))["result"][0]
    except Exception:
        return None
    coords = []
    for m in r.get("maneuvers", []):
        for g in (m.get("outcoming_path") or {}).get("geometry", []):
            for xy in re.findall(r"(-?\d+\.\d+) (-?\d+\.\d+)", g.get("selection", "")):
                pt = [round(float(xy[0]), 5), round(float(xy[1]), 5)]
                if not coords or coords[-1] != pt:
                    coords.append(pt)
    if len(coords) < 2:
        return None
    res = {"km": r["total_distance"] / 1000, "min": r["total_duration"] / 60, "coords": coords, "src": "2GIS"}
    with _lock:
        _cache[key] = res
    return res


def geocode(text: str) -> dict | None:
    """Поиск места/адреса в Астане через 2GIS — понимает сокращения и разговорные названия («мну универ», «тц хан»)."""
    if not _alive():
        return None
    key = "g:" + text.lower().strip()
    if key not in _cache:
        if _cache.get("_count", 0) >= BUDGET or not _slot("places"):
            return None
        with _lock:
            _cache["_count"] = _cache.get("_count", 0) + 1
        try:
            r = _check(httpx.get(URL, timeout=6, params={"q": text, "location": "71.43,51.13", "point": "71.43,51.13",
                                                         "radius": 40000, "page_size": 1, "fields": "items.point",
                                                         "locale": "ru_KZ", "key": KEY}))
            if r is None:
                return None
            items = r.get("result", {}).get("items", [])
        except Exception:
            return None
        it = items[0] if items and (items[0].get("point") or {}).get("lat") else None
        with _lock:
            _cache[key] = {"lat": it["point"]["lat"], "lon": it["point"]["lon"], "name": it.get("name") or it.get("full_name")} if it else {}
        save()
    d = _cache[key]
    return {"lat": d["lat"], "lon": d["lon"], "label": d["name"], "source": "поиск 2GIS"} if d else None
