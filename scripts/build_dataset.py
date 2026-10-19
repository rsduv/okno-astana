"""Открытый датасет мест Астаны в формате из ТЗ: название, координаты, короткое описание, категория/теги,
часы работы, контакты, популярность — и у каждого поля указано, откуда оно взялось.

Собирается автоматически из того, что уже скачали скрипты сбора (OSM → data/poi.json, Wikidata/Википедия → data/wiki.json).
Данные 2GIS сюда НЕ попадают (их условия не разрешают публиковать).

    python scripts/build_dataset.py
Результат: data/dataset/astana_poi.csv, data/dataset/astana_poi.jsonl, docs/dataset.md (паспорт датасета).
"""
import csv
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

os.environ.setdefault("DGIS_OFF", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import planner  # noqa: E402  (тот же разбор, что и в продукте: места из Википедии, площадки, точки в ТРЦ)
from app import geo  # noqa: E402
from app.hours import OUTDOOR, TYPICAL  # noqa: E402

LANDMARKS = json.loads((ROOT / "data" / "landmarks.json").read_text(encoding="utf-8"))
_d, _l = ROOT / "data" / "districts.json", ROOT / "data" / "lrt.json"   # scripts/collect_city.py
DISTRICTS = json.loads(_d.read_text(encoding="utf-8")) if _d.exists() else []
LRT = json.loads(_l.read_text(encoding="utf-8"))["stations"] if _l.exists() else []


def district(lat, lon) -> str:
    from scripts.collect_city import inside
    return next((d["name"] for d in DISTRICTS if inside(lat, lon, d["segments"])), "")


def lrt_near(lat, lon):
    """Ближайшая станция LRT (линия открыта 16.05.2026) — если до неё не больше 1,2 км."""
    if not LRT:
        return "", ""
    st = min(LRT, key=lambda x: geo.haversine_km((lat, lon), (x["lat"], x["lon"])))
    d = geo.haversine_km((lat, lon), (st["lat"], st["lon"])) * 1000
    return (st["name"], int(round(d, -1))) if d <= 1200 else ("", "")

# «что делать там» — типичное действие для категории (шаблон, не ИИ; продукт уточняет его под запрос)
TODO = {
    "кафе": "взять кофе с собой или посидеть 20–30 мин", "ресторан": "пообедать или поужинать, 45–60 мин",
    "фастфуд": "быстро перекусить, 15–20 мин", "бар": "вечер с друзьями", "ночной клуб": "потанцевать поздно вечером",
    "торговый центр": "погреться, поесть на фудкорте, кино и магазины", "музей": "посмотреть экспозицию, 45–60 мин",
    "галерея": "посмотреть выставку, 30 мин", "арт-центр": "выставка или мастер-класс", "театр": "спектакль по афише",
    "кино": "сеанс фильма", "библиотека": "почитать, позаниматься в тишине", "книжный": "полистать книги, выбрать подарок",
    "коворкинг": "поработать с ноутбуком", "памятник": "сфотографироваться, 5–10 мин",
    "достопримечательность": "посмотреть и сфотографироваться", "фонтан": "пройтись и сфотографироваться (летом)",
    "смотровая": "посмотреть на город сверху", "парк": "прогуляться 20–40 мин", "сад": "спокойная прогулка",
    "детская площадка": "дать ребёнку побегать 20 мин", "мечеть": "посмотреть архитектуру (скромная одежда)",
    "собор": "посмотреть архитектуру", "каток": "покататься на коньках", "боулинг": "сыграть пару партий компанией",
    "квест": "пройти квест компанией", "развлечения": "аттракционы и игры", "океанариум": "посмотреть морских животных",
    "зоопарк": "посмотреть животных", "парк развлечений": "аттракционы", "батутный парк": "попрыгать с детьми",
    "аквапарк": "искупаться, 1,5–3 ч", "детский центр": "игровая зона для детей",
}

OUT = ROOT / "data" / "dataset"

FEATURES = {  # тег OSM → человеческий признак
    ("outdoor_seating", "yes"): "летняя веранда", ("internet_access", "wlan"): "Wi-Fi", ("internet_access", "yes"): "интернет",
    ("wheelchair", "yes"): "доступно на коляске", ("wheelchair", "limited"): "частично доступно на коляске",
    ("wheelchair", "no"): "недоступно на коляске", ("fee", "no"): "вход бесплатный", ("fee", "yes"): "вход платный",
}
CUISINE = {"coffee_shop": "кофейня", "burger": "бургеры", "pizza": "пицца", "sushi": "суши", "kazakh": "казахская кухня",
           "regional": "местная кухня", "italian": "итальянская", "japanese": "японская", "asian": "азиатская",
           "chinese": "китайская", "korean": "корейская", "turkish": "турецкая", "uzbek": "узбекская",
           "georgian": "грузинская", "russian": "русская", "american": "американская", "chicken": "курица",
           "kebab": "кебаб", "doner": "донер", "shawarma": "шаурма", "steak_house": "стейки", "ice_cream": "мороженое",
           "cake": "десерты", "dessert": "десерты", "tea": "чай", "bubble_tea": "бабл-ти", "seafood": "морепродукты",
           "european": "европейская", "international": "интернациональная", "sandwich": "сэндвичи", "breakfast": "завтраки",
           "bakery": "выпечка", "fish": "рыба", "noodle": "лапша", "indian": "индийская", "arab": "арабская",
           "middle_eastern": "ближневосточная", "lebanese": "ливанская", "french": "французская", "mexican": "мексиканская",
           "uyghur": "уйгурская", "tatar": "татарская", "azerbaijani": "азербайджанская"}


def describe(p: dict) -> tuple[str, str]:
    """Короткое описание и его источник: Википедия → описание из OSM → собранное из тегов OSM (без выдумок)."""
    t = p.get("tags") or {}
    if (p.get("wiki") or {}).get("extract"):
        ext = p["wiki"]["extract"].strip()
        return (ext[:280].rsplit(" ", 1)[0] + "…" if len(ext) > 280 else ext), "wikipedia"
    if t.get("description:ru") or t.get("description"):
        return (t.get("description:ru") or t["description"])[:280], "osm:description"
    bits = [p["category"].capitalize()]
    if p.get("inside"):
        bits[0] += f" внутри «{p['inside']}»"
    cu = [CUISINE.get(c.strip(), None) for c in t.get("cuisine", "").split(";") if c.strip()]
    cu = [c for c in cu if c]
    if cu:
        bits.append("кухня: " + ", ".join(dict.fromkeys(cu)))
    if t.get("brand"):
        bits.append(f"сеть «{t['brand']}»")
    feats = [v for (k, val), v in FEATURES.items() if t.get(k) == val]
    if feats:
        bits.append(", ".join(feats))
    addr = address(t)
    if addr:
        bits.append("адрес: " + addr)
    near = nearest_landmark(p)
    if near:
        bits.append(f"рядом {near[0]} ({near[1]} м)")
    st, m = lrt_near(p["lat"], p["lon"])
    if st:
        bits.append(f"станция LRT «{st}» — {m} м")
    dn = district(p["lat"], p["lon"])
    if dn:
        bits.append(f"район {dn}")
    return ". ".join(b[0].upper() + b[1:] for b in bits) + ".", "osm:tags"


def nearest_landmark(p: dict):
    """Ближайший известный ориентир — чтобы по описанию было понятно, где это («рядом Хан Шатыр, 300 м»)."""
    best = min(LANDMARKS, key=lambda m: geo.haversine_km((p["lat"], p["lon"]), (m["lat"], m["lon"])))
    d = geo.haversine_km((p["lat"], p["lon"]), (best["lat"], best["lon"])) * 1000
    if d < 30 or p["name"] == best["name"]:
        return None
    return (best["name"], int(round(d, -1))) if d <= 1500 else None


def address(t: dict) -> str:
    return ", ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)


def hours(p: dict) -> tuple[str, str]:
    t = p.get("tags") or {}
    if t.get("opening_hours"):
        return t["opening_hours"], ("osm:mall" if p.get("hours_from_mall") else "osm")
    if p["category"] in OUTDOOR:
        return "24/7", "outdoor (открытое место; ночью и в мороз продукт это учитывает)"
    if p["category"] in TYPICAL:
        return TYPICAL[p["category"]], "typical-for-category (не проверено)"
    return "", "unknown"


def popularity(p: dict) -> tuple[float, str]:
    """0..1. Рейтингов в открытых данных нет — честный заменитель: на скольких языках есть статья в Википедии
    и насколько подробно место описано в OSM. Это «известность», а не оценка качества."""
    sl = (p.get("wiki") or {}).get("sitelinks") or 0
    rich = min(p.get("richness", 0), 10)
    score = min(1.0, sl / 40) * 0.7 + rich / 10 * 0.3
    return round(score, 3), ("wikipedia-languages+osm-detail" if sl else "osm-detail")


def main():
    rows = []
    for p in planner.POIS:
        if "2gis" in (p.get("source") or "") or str(p.get("id", "")).startswith("2gis"):
            continue
        t = p.get("tags") or {}
        desc, dsrc = describe(p)
        oh, hsrc = hours(p)
        pop, psrc = popularity(p)
        tags = sorted({p["category"]} | {v for (k, val), v in FEATURES.items() if t.get(k) == val} |
                      {CUISINE[c.strip()] for c in t.get("cuisine", "").split(";") if c.strip() in CUISINE})
        rows.append({
            "id": p["id"], "name": p["name"], "name_en": p.get("name_en") or "", "name_kk": p.get("name_kk") or "",
            "lat": round(p["lat"], 5), "lon": round(p["lon"], 5), "category": p["category"], "tags": "; ".join(tags),
            "description": desc, "description_source": dsrc,
            "what_to_do": TODO.get(p["category"], ""), "visit_min": planner.STAY.get(p["category"], 30),
            "near_landmark": (nearest_landmark(p) or ("", ""))[0], "near_landmark_m": (nearest_landmark(p) or ("", ""))[1],
            "district": district(p["lat"], p["lon"]),
            "lrt_station": lrt_near(p["lat"], p["lon"])[0], "lrt_station_m": lrt_near(p["lat"], p["lon"])[1],
            "opening_hours": oh, "hours_source": hsrc,
            "phone": t.get("contact:phone") or t.get("phone") or "", "website": t.get("contact:website") or t.get("website") or "",
            "instagram": t.get("contact:instagram") or "", "address": address(t),
            "popularity": pop, "popularity_source": psrc,
            "wikipedia": (p.get("wiki") or {}).get("article") or "", "source_url": p.get("source") or "",
        })
    rows.sort(key=lambda r: (r["category"], r["name"]))
    dist = Counter(r["district"] or "вне границ" for r in rows)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "astana_poi.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(OUT / "astana_poi.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(rows)
    cats = Counter(r["category"] for r in rows)
    pct = lambda k: f"{sum(1 for r in rows if r[k]) / n:.0%}"  # noqa: E731
    src = lambda k: Counter(r[k] for r in rows)  # noqa: E731
    lines = [
        "# Датасет мест Астаны («Окно»)", "",
        f"Собран автоматически {date.today():%d.%m.%Y}. Мест: **{n}**. Файлы: `data/dataset/astana_poi.csv` (открывается в Excel) "
        "и `data/dataset/astana_poi.jsonl`.", "",
        "## Как собирается (без ручной работы)", "",
        "1. `scripts/collect_osm.py` — запрос в OpenStreetMap (Overpass API) по прямоугольнику Астаны: кафе, рестораны, музеи, "
        "парки, ТРЦ, памятники, площадки и т.д. → `data/poi.json`.",
        "2. `scripts/collect_wiki.py` — Wikidata/Википедия: известные места, описание, фото, названия на EN/KZ → `data/wiki.json`.",
        "3. `scripts/collect_city.py` — границы 6 районов Астаны и станции LRT из OSM → `data/districts.json`, `data/lrt.json`.",
        "4. `scripts/collect_events.py` — афиша Astana Opera → `data/events.json` (ежедневно).",
        "5. `scripts/build_dataset.py` — сводит всё в одну таблицу с полями из ТЗ и источником каждого поля.",
        "", "GitHub Actions пересобирает базу каждый понедельник (`update-poi.yml`) и афишу каждый день (`update-events.yml`).",
        "Данные 2GIS продукт использует для уточнения часов/рейтинга на лету, но в открытый датасет их не кладём — "
        "условия 2GIS не разрешают публикацию.", "",
        "## Поля", "",
        "| Поле из ТЗ | Колонки | Заполнено | Откуда |", "|---|---|---|---|",
        f"| Название | name, name_en, name_kk | 100% / {pct('name_en')} / {pct('name_kk')} | OSM, Wikidata |",
        "| Координаты | lat, lon | 100% | OSM / Wikidata |",
        f"| Короткое описание | description, description_source | 100% | {', '.join(f'{k}: {v}' for k, v in src('description_source').most_common())} |",
        f"| Категория / теги | category, tags | 100% | теги OSM → {len(cats)} категорий |",
        f"| Время работы | opening_hours, hours_source | {pct('opening_hours')} | "
        f"{', '.join(f'{k}: {v}' for k, v in src('hours_source').most_common())} |",
        f"| Контакты | phone, website, instagram, address | тел. {pct('phone')}, сайт {pct('website')}, адрес {pct('address')} | OSM |",
        "| Что делать / сколько времени | what_to_do, visit_min | 100% | шаблон по категории (продукт уточняет под запрос) |",
        f"| Где это | near_landmark, near_landmark_m | {pct('near_landmark')} | ближайший из 32 ориентиров, до 1,5 км |",
        f"| Район города | district | {pct('district')} | границы районов из OSM (`scripts/collect_city.py`) |",
        f"| Транспорт | lrt_station, lrt_station_m | {pct('lrt_station')} | ближайшая станция LRT до 1,2 км (OSM, линия открыта 16.05.2026) |",
        f"| Рейтинг / популярность | popularity (0–1), popularity_source | 100% | статья в Википедии на N языках + подробность в OSM |",
        "", "**Честно о дырах:** у части мест часы — типичные для категории, это помечено в `hours_source` "
        "(«не проверено»), и продукт в этом случае пишет «обычно открыто», а не «открыто». Рейтингов в открытых данных нет: "
        "`popularity` — это известность места, а не оценка. Описание `osm:tags` собрано из фактов OSM (кухня, веранда, "
        "Wi-Fi, адрес) — ничего не придумано ИИ.", "",
        "## Районы", "", "| Район | Мест |", "|---|---|", *[f"| {k} | {v} |" for k, v in dist.most_common()], "",
        "## Категории", "", "| Категория | Мест |", "|---|---|",
        *[f"| {c} | {k} |" for c, k in cats.most_common()], "",
        "## Пример строк", "",
        "| name | category | description | opening_hours | popularity |", "|---|---|---|---|---|",
        *[f"| {r['name']} | {r['category']} | {r['description'][:90]} | {r['opening_hours'] or '—'} ({r['hours_source']}) | {r['popularity']} |"
          for r in sorted(rows, key=lambda r: -r["popularity"])[:8]],
        "", "Лицензии: OpenStreetMap — ODbL (© участники OpenStreetMap), Wikidata — CC0, тексты Википедии — CC BY-SA.", "",
    ]
    (ROOT / "docs" / "dataset.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"{n} мест, {len(cats)} категорий; описание {dict(src('description_source'))}; часы {dict(src('hours_source'))}")


if __name__ == "__main__":
    main()
