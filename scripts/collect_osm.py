"""Автоматический сбор точек интереса (POI) Астаны из OpenStreetMap через Overpass API.

Запуск:  python scripts/collect_osm.py
Результат: data/poi_raw.json (ответ Overpass как есть) и data/poi.json (очищенные POI).
Только стандартная библиотека — ничего ставить не нужно.
"""
import json
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Прямоугольник вокруг Астаны: юг, запад, север, восток
BBOX = "51.03,71.20,51.28,71.65"

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Какие объекты берём: ключ OSM -> значения
SELECTORS = {
    "amenity": "cafe|restaurant|fast_food|bar|pub|nightclub|cinema|theatre|arts_centre|library|ice_cream|planetarium|fountain|coworking_space",
    "tourism": "museum|gallery|attraction|viewpoint|zoo|theme_park|aquarium",
    "leisure": "park|garden|playground|water_park|amusement_arcade|escape_game|bowling_alley|ice_rink|trampoline_park",
    "shop": "mall|books",
    "historic": "monument|memorial",
    "building": "mosque|cathedral",
}

# Внутренняя категория для каждой пары ключ=значение
CATEGORY = {
    "cafe": "кафе", "restaurant": "ресторан", "fast_food": "фастфуд", "ice_cream": "кафе",
    "bar": "бар", "pub": "бар", "nightclub": "ночной клуб",
    "cinema": "кино", "theatre": "театр", "arts_centre": "арт-центр", "planetarium": "музей",
    "library": "библиотека", "coworking_space": "коворкинг", "fountain": "фонтан",
    "museum": "музей", "gallery": "галерея", "attraction": "достопримечательность",
    "viewpoint": "смотровая", "zoo": "зоопарк", "theme_park": "парк развлечений", "aquarium": "океанариум",
    "park": "парк", "garden": "сад", "playground": "детская площадка", "water_park": "аквапарк",
    "amusement_arcade": "развлечения", "escape_game": "квест", "bowling_alley": "боулинг",
    "ice_rink": "каток", "trampoline_park": "батутный парк",
    "mall": "торговый центр", "books": "книжный",
    "monument": "памятник", "memorial": "памятник",
    "mosque": "мечеть", "cathedral": "собор",
}

# Теги, которые полезны для рекомендаций (если есть)
USEFUL = [
    "opening_hours", "website", "contact:website", "phone", "contact:phone", "contact:instagram",
    "wheelchair", "outdoor_seating", "internet_access", "cuisine", "description", "description:ru",
    "addr:street", "addr:housenumber", "wikidata", "wikipedia", "fee", "level", "brand",
]


def build_query() -> str:
    parts = [f'nwr["{k}"~"^({v})$"]({BBOX});' for k, v in SELECTORS.items()]
    return "[out:json][timeout:180];(\n" + "\n".join(parts) + "\n);out center tags;"


def fetch(query: str) -> dict:
    body = urllib.parse.urlencode({"data": query}).encode()
    last_err = None
    for url in ENDPOINTS:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": "astana-walk-helper/0.1 (hackathon)"})
                with urllib.request.urlopen(req, timeout=240) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:  # сеть/лимиты Overpass — пробуем ещё раз или другой сервер
                last_err = e
                print(f"  {url} попытка {attempt + 1}: {e}")
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Overpass недоступен: {last_err}")


def normalize(el: dict) -> dict | None:
    tags = el.get("tags", {})
    name = tags.get("name:ru") or tags.get("name") or tags.get("name:kk") or tags.get("name:en")
    if not name and tags.get("leisure") == "playground":
        name = "Детская площадка"  # площадки почти всегда без названия, но для сценария «с ребёнком» они нужны
    if not name:
        return None  # остальные безымянные точки рекомендовать нельзя
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    if lat is None or lon is None:
        return None
    kind = None
    for k in SELECTORS:
        v = tags.get(k)
        if v in CATEGORY:
            kind = (k, v)
            break
    if not kind:
        return None
    # Популярность в OSM напрямую не хранится — грубая оценка «насколько точка известна/описана»
    richness = sum(1 for t in USEFUL if t in tags) + (3 if "wikidata" in tags else 0)
    return {
        "id": f"{el['type']}/{el['id']}",
        "name": name,
        "name_en": tags.get("name:en"),
        "name_kk": tags.get("name:kk"),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "category": CATEGORY[kind[1]],
        "osm_tag": f"{kind[0]}={kind[1]}",
        "tags": {t: tags[t] for t in USEFUL if t in tags},
        "richness": richness,
        "source": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
    }


def main():
    DATA.mkdir(exist_ok=True)
    print("Запрашиваю Overpass API...")
    raw = fetch(build_query())
    (DATA / "poi_raw.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    seen, pois = set(), []
    for el in raw.get("elements", []):
        p = normalize(el)
        if not p:
            continue
        key = (p["name"].lower(), round(p["lat"], 4), round(p["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        pois.append(p)

    (DATA / "poi.json").write_text(json.dumps(pois, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Всего элементов от OSM: {len(raw.get('elements', []))}, POI с названием: {len(pois)}")
    for cat, n in Counter(p["category"] for p in pois).most_common():
        print(f"  {cat}: {n}")
    with_hours = sum(1 for p in pois if "opening_hours" in p["tags"])
    print(f"С часами работы: {with_hours} ({100 * with_hours // max(len(pois), 1)}%)")


if __name__ == "__main__":
    main()
