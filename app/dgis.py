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


def enabled() -> bool:
    return bool(KEY) and os.getenv("DGIS_OFF") != "1" and _cache.get("_count", 0) < BUDGET


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
    with _lock:
        _cache["_count"] = _cache.get("_count", 0) + 1
    try:
        r = httpx.get(URL, timeout=6, params={"q": p["name"], "point": f"{p['lon']},{p['lat']}", "radius": 200,
                                              "page_size": 5, "fields": FIELDS, "locale": "ru_KZ", "key": KEY})
        items = r.json().get("result", {}).get("items", [])
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
