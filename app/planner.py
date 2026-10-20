"""Планировщик «Окна»: из точки А в точку Б к сроку, с 1–3 остановками, которые точно успеваешь.

Это «R» в RAG: жёсткие проверки делает код (время, часы, погода, расстояния), а ИИ потом только
объясняет выбор человеческим языком — и только по фактам, которые вернул планировщик.
"""
from __future__ import annotations

import itertools
import time
import os
from concurrent.futures import ThreadPoolExecutor
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import dgis, geo
from .hours import OUTDOOR, interval_status, next_open

ROOT = Path(__file__).resolve().parent.parent
POIS: list[dict] = json.loads((ROOT / "data" / "poi.json").read_text(encoding="utf-8"))


# главная рубрика 2GIS, при которой место действительно подходит категории
RUBRICS_OK = {
    "кафе": {"Кофейни", "Кафе", "Кафе-кондитерские", "Кондитерские изделия", "Пекарни"},
    "бар": {"Бары", "Рестораны", "Кафе", "Караоке-залы", "Ночные клубы", "Пабы"},
    "ресторан": {"Рестораны", "Кафе", "Суши-бары", "Караоке-залы"},
    "музей": {"Музеи", "Интересные здания"},
    "галерея": {"Художественные выставки", "Музеи"},
    "развлечения": {"Детские игровые залы", "Аттракционы", "Батутные центры", "Батутные парки"},
    "кино": {"Кинотеатры"},
    "коворкинг": {"Коворкинг"},
}


def _merge_2gis() -> int:
    """Популярные места из 2GIS (scripts/collect_2gis.py, локальный файл): если место уже есть в OSM — даём ему
    рейтинг и свежие часы; если нет — добавляем как новое с источником 2GIS. Без файла система работает на одном OSM."""
    # локально — data/dgis_pois.json; на хостинге — «секретный файл» (на Render: /etc/secrets/dgis_pois.json)
    f = next((x for x in (Path(os.getenv("DGIS_POIS_PATH", "")), ROOT / "data" / "dgis_pois.json",
                          Path("/etc/secrets/dgis_pois.json")) if str(x) not in ("", ".") and x.is_file()), ROOT / "data" / "dgis_pois.json")
    if not f.exists():
        return 0
    grid: dict[tuple, list] = {}
    for p in POIS:
        grid.setdefault((round(p["lat"], 2), round(p["lon"], 2)), []).append(p)
    added = 0
    for it in json.loads(f.read_text(encoding="utf-8")).values():
        rub = (it.get("rubrics") or [""])[0]
        if rub not in RUBRICS_OK.get(it["category"], {rub}):
            continue  # по запросу «галерея» 2GIS отдаёт и арт-магазины, и курсы — берём только то, куда идут гулять
        d = dgis.parse(it)
        name = it["name"].split(",")[0].strip()
        if not name:
            continue
        n = dgis._norm(name)
        best = None
        for dy in (-0.01, 0, 0.01):
            for dx in (-0.01, 0, 0.01):
                for p in grid.get((round(it["lat"] + dy, 2), round(it["lon"] + dx, 2)), []):
                    m = dgis._norm(p["name"])
                    if geo.haversine_km((p["lat"], p["lon"]), (it["lat"], it["lon"])) <= 0.1 and n and m and (
                            n in m or m in n or n[:5] == m[:5]):
                        best = p
        if best is None:
            best = {"id": "2gis/" + it["id"].split("_")[0], "name": name, "name_en": None, "name_kk": None,
                    "lat": it["lat"], "lon": it["lon"], "category": it["category"], "osm_tag": None, "tags": {},
                    "richness": 3, "source": f"https://2gis.kz/astana/firm/{it['id'].split('_')[0]}"}
            POIS.append(best)
            added += 1
        if rub == "Детские игровые залы":
            best["category"] = "детский центр"  # только для запросов с детьми, не для «каток вечером»
        best["dgis"] = d
        if d.get("hours"):
            best.setdefault("osm_hours", best["tags"].get("opening_hours"))
            best["tags"] = {**best["tags"], "opening_hours": d["hours"]}
            best["hours_src"] = "2GIS"
    return added


DGIS_ADDED = _merge_2gis()


def _grp(cat: str) -> str:
    return {"памятник": "s", "достопримечательность": "s", "смотровая": "s", "мечеть": "w", "собор": "w",
            "музей": "c", "театр": "c", "галерея": "c", "библиотека": "c", "кино": "c"}.get(cat, cat)


def _merge_wiki() -> int:
    """Известные места из Wikidata/Википедии (scripts/collect_wiki.py): сигнал известности (на скольких языках
    есть статья) и короткое описание — факты для «почему». Нет места в OSM — добавляем с источником Википедия."""
    f = ROOT / "data" / "wiki.json"
    if not f.exists():
        return 0
    added = 0
    for w in json.loads(f.read_text(encoding="utf-8")).values():
        if w["name"].startswith("Q") or w["name"].lower().startswith("ограждение") or (w["sitelinks"] < 1 and not w.get("extract")):
            continue
        n = dgis._norm(w["name"])
        best, bd = None, 9.0
        for p in POIS:
            d = geo.haversine_km((p["lat"], p["lon"]), (w["lat"], w["lon"]))
            if d > 0.35:
                continue
            m = dgis._norm(p["name"])
            same = (p["tags"].get("wikidata") == w["qid"]) or (
                _grp(p["category"]) == _grp(w["category"]) and len(m) >= 5 and n and (n[:6] in m or m[:6] in n))
            if same and d < bd:
                best, bd = p, d
        name = re.sub(r"\s*\([^)]*\)", "", w["name"]).strip()  # «Ак орда (резиденция)» → «Ак орда»
        if best is None:
            if w["sitelinks"] < 2 or any(q.get("id") == "wikidata/" + w["qid"] for q in POIS):
                continue
            best = {"id": "wikidata/" + w["qid"], "name": name, "name_en": None, "name_kk": None, "lat": w["lat"],
                    "lon": w["lon"], "category": w["category"], "osm_tag": None, "tags": {"wikidata": w["qid"]},
                    "richness": 4, "source": w.get("article") or f"https://www.wikidata.org/wiki/{w['qid']}"}
            POIS.append(best)
            added += 1
        ext = (w.get("extract") or "").replace(chr(0x301), "") or None  # убираем знаки ударения из Википедии
        best["wiki"] = {"sitelinks": w["sitelinks"], "extract": ext, "article": w.get("article"), "image": w.get("image")}
        best["tags"].setdefault("wikidata", w["qid"])
        for lang in ("en", "kk"):
            if not best.get(f"name_{lang}") and w.get(f"name_{lang}"):
                best[f"name_{lang}"] = re.sub(r"\s*\([^)]*\)|,\s*(Astana|Nur-Sultan|Астана)$", "", w[f"name_{lang}"]).strip()
    return added


WIKI_ADDED = _merge_wiki()


def _named_playgrounds():
    """«Детская площадка» с собственным названием (Gymboree) — это детский клуб в помещении, а не площадка во дворе."""
    for p in POIS:
        if p["category"] == "детская площадка" and not p.get("unnamed") and not p["name"].lower().startswith("детская"):
            p["category"] = "развлечения"


_named_playgrounds()


def _inside_malls():
    """Аттракционы, отмеченные в OSM точками внутри ТРЦ (горки в Хан Шатыре и т.п.), — это не уличные
    достопримечательности: они работают по часам ТРЦ. Переносим часы ТРЦ и помечаем «внутри»."""
    malls = [p for p in POIS if p["category"] == "торговый центр"]
    for p in POIS:
        if p["category"] not in ("достопримечательность", "развлечения", "парк развлечений", "кафе", "ресторан",
                                 "фастфуд", "кино", "детский центр", "бар", "галерея", "батутный парк"):
            continue
        for m in malls:
            if geo.haversine_km((p["lat"], p["lon"]), (m["lat"], m["lon"])) < 0.15:
                p["inside"] = m["name"]
                if p["category"] == "достопримечательность":
                    p["category"] = "развлечения"
                if "opening_hours" not in p["tags"] and "opening_hours" in m["tags"]:
                    p["tags"]["opening_hours"] = m["tags"]["opening_hours"]
                    p["hours_from_mall"] = True
                break


_inside_malls()


def _name_playgrounds():
    """Безымянные площадки называем по ближайшему известному месту — «Детская площадка у Керуена»,
    чтобы человек понимал, куда идти, а две площадки в маршруте не выглядели одинаково."""
    named = [p for p in POIS if p["category"] != "детская площадка" and len(p["name"]) <= 24]
    for p in POIS:
        if p["name"] != "Детская площадка":
            continue
        pt = (p["lat"], p["lon"])
        best = min(named, key=lambda q: geo.haversine_km(pt, (q["lat"], q["lon"])))
        p["unnamed"] = True
        if geo.haversine_km(pt, (best["lat"], best["lon"])) < 0.35:
            p["name"] = f"Детская площадка у «{best['name']}»"


_name_playgrounds()

# Сколько обычно проводят времени в месте, минут
STAY = {
    "кафе": 25, "ресторан": 50, "фастфуд": 20, "бар": 45, "ночной клуб": 90, "торговый центр": 25,
    "музей": 45, "галерея": 30, "арт-центр": 30, "театр": 120, "кино": 120, "библиотека": 30,
    "книжный": 20, "коворкинг": 60, "памятник": 10, "достопримечательность": 15, "фонтан": 10,
    "смотровая": 15, "парк": 25, "сад": 20, "детская площадка": 20, "мечеть": 20, "собор": 20,
    "каток": 45, "боулинг": 60, "квест": 60, "развлечения": 40, "океанариум": 60, "зоопарк": 60,
    "парк развлечений": 45, "батутный парк": 45, "аквапарк": 90, "детский центр": 45,
}
PREF_CATS = {
    "coffee": {"кафе"},
    "food": {"ресторан", "кафе", "фастфуд"},
    "sights": {"достопримечательность", "памятник", "смотровая", "фонтан", "мечеть", "собор"},
    "nature": {"парк", "сад"},
    "culture": {"музей", "галерея", "арт-центр", "библиотека", "театр"},
    "shopping": {"торговый центр", "книжный"},
    "kids": {"детская площадка", "детский центр", "парк развлечений", "развлечения", "батутный парк", "океанариум",
             "зоопарк", "каток", "аквапарк", "парк", "торговый центр"},
    "night": {"бар", "ночной клуб", "боулинг", "кино", "квест", "достопримечательность", "смотровая", "фонтан"},
    "work": {"кафе", "коворкинг", "библиотека"},
    "active": {"каток", "боулинг", "квест", "развлечения", "батутный парк"},
    "cinema": {"кино"},
    "walk": {"парк", "сад", "достопримечательность", "памятник", "фонтан", "смотровая"},
    "unusual": {"квест", "развлечения", "океанариум", "арт-центр", "галерея", "смотровая", "боулинг", "каток", "памятник"},
    "romantic": {"смотровая", "ресторан", "фонтан", "парк", "театр", "кафе", "памятник"},
    "view": {"смотровая", "памятник", "достопримечательность"},
    "quiet": {"библиотека", "галерея", "музей", "сад", "парк", "кафе", "книжный"},
    "dog": {"парк", "сад", "фонтан", "памятник", "достопримечательность", "смотровая"},
    "indoor": {"торговый центр", "музей", "галерея", "кафе", "развлечения", "кино", "библиотека", "океанариум",
               "батутный парк", "аквапарк", "каток", "боулинг", "книжный"},
}
UNMET = {"coffee": "Кафе", "food": "Места поесть", "work": "Места для работы", "culture": "Музея или галереи",
         "cinema": "Кино", "kids": "Детского места", "night": "Бара или ночного места", "shopping": "Магазина или ТРЦ"}
DEFAULT_CATS = {"кафе", "достопримечательность", "памятник", "парк", "сад", "торговый центр", "музей",
                "галерея", "смотровая", "фонтан", "арт-центр", "книжный", "мечеть"}
LONG_STAYS = {"театр", "кино", "ночной клуб", "аквапарк"}


def _fmt(t: datetime) -> str:
    return t.strftime("%H:%M")


class Ctx:
    def __init__(self, intent: dict, now: datetime, w: dict):
        self.i = intent
        self.now = now
        self.w = w
        self.cold = geo.cold_level(w)
        self.max_leg = geo.MAX_OUTDOOR_LEG[self.cold]
        if intent.get("mobility") == "limited":
            self.max_leg = min(self.max_leg, 10)   # пожилым и на коляске — длинные куски лучше на такси
        self.rejected: dict[str, int] = {}
        self.rejected_ex: dict[str, list] = {}
        self.rejected_seen: set = set()
        # темп: коляска / трудно ходить — медленнее и короче отрезки; с ребёнком — тоже медленнее
        self.pace = 1.0
        if intent.get("mobility") == "limited":
            self.pace, self.max_leg = 1.35, min(self.max_leg, 10)
        elif intent.get("with_kids"):
            self.pace = 1.15
        self.prefs = set(intent.get("prefs") or [])
        if intent.get("with_kids"):
            self.prefs.add("kids")
        far = geo.haversine_km((intent["origin"]["lat"], intent["origin"]["lon"]),
                               (intent["destination"]["lat"], intent["destination"]["lon"])) > 3
        # «погулять» по пути в аэропорт не значит «идти до аэропорта пешком»
        at_hub = re.search(r"Аэропорт|Вокзал", (intent.get("origin") or {}).get("label", "")) is not None
        # из аэропорта/вокзала в город пешком не гуляют — там нужно такси, даже если человек хочет «погулять»
        self.walk_only = intent.get("transport") == "walk" or ("walk" in self.prefs and not far and not at_hub)
        if intent.get("mobility") == "limited":
            self.walk_only = False   # «прогуляться, но не далеко» пожилому — дальние куски на такси, а не час пешком
        self.work = "work" in self.prefs
        self.work_stay = 60
        self.real_now = datetime.now(timezone(timedelta(hours=5))).replace(tzinfo=None)
        sunset = w.get("sunset")
        self.dark_from = None
        if sunset:
            h, m = map(int, sunset.split(":"))
            self.dark_from = now.replace(hour=h, minute=m, second=0, microsecond=0)
        self.wanted = set().union(*(PREF_CATS[p] for p in self.prefs if p in PREF_CATS)) if self.prefs else set(DEFAULT_CATS)
        self.explicit = set(intent.get("explicit") or [])
        self.wanted |= self.explicit
        if self.prefs and not (self.prefs & {"kids", "night", "work", "cinema", "active", "unusual", "view"}):
            self.wanted |= {"кафе", "достопримечательность", "памятник"}  # всегда есть чем «добить» окно

    def is_dark(self, t: datetime) -> bool:
        if self.dark_from is None:
            return t.hour >= 19 or t.hour < 7
        return t >= self.dark_from or t.hour < 7


# короткий заход (10 мин) имеет смысл для парка или памятника, но не для боулинга или кино
# музеи под открытым небом (в OSM — tourism=museum, но гуляешь по улице): «Атамекен» — карта Казахстана в миниатюре
OPEN_AIR = re.compile(r"атамекен|карта казахстана|под открытым небом|open[- ]air", re.I)


def is_outdoor(p: dict) -> bool:
    return p["category"] in OUTDOOR or bool(OPEN_AIR.search(p["name"]))


SHORT_OK = {"парк", "сад", "памятник", "достопримечательность", "смотровая", "мечеть", "собор", "галерея", "музей", "фонтан"}
VIEW_POINTS = {"Байтерек"}  # смотровая площадка на высоте 97 м (Википедия); в OSM размечен как памятник
GENERIC = {"сквер", "парк", "фонтан", "памятник", "скульптура", "монумент", "кафе", "столовая", "бар", "ресторан",
           "кофейня", "кафетерий", "буфет", "сад", "аллея"}


def stop_score(p: dict, ctx: Ctx, arrive: datetime | None = None) -> float | None:
    cat = p["category"]
    tags = p.get("tags", {})
    if cat not in ctx.wanted:
        return None
    if "kids" not in ctx.prefs and re.search(r"детск|балалар|children", p["name"].lower()) and cat != "детская площадка":
        return None  # «Детская библиотека» взрослому, который хочет почитать, — мимо
    if cat == "фонтан" and not 5 <= ctx.now.month <= 9:
        return None  # фонтаны в Астане работают с мая по сентябрь
    if p["name"].strip().lower() in GENERIC:
        return None  # «Сквер» без названия: человек не найдёт и не поймёт, куда идти
    s = 1.0
    if ctx.prefs and any(cat in PREF_CATS.get(pr, ()) for pr in ctx.prefs):
        s += 1.5
    if cat in ctx.explicit:
        s += 3.0  # человек прямо назвал тип места
    if "view" in ctx.prefs and p["name"] in VIEW_POINTS:
        s += 2.5  # смотровая площадка — именно «вид на город»
    if "dog" in ctx.prefs and cat not in OUTDOOR:
        return None  # с собакой в кафе и ТРЦ обычно нельзя — гуляем на улице
    if "dog" in ctx.prefs and cat in ("парк", "сад"):
        s += 2.0   # собаке нужна трава и простор, а не площадь у памятника
    if "indoor" in ctx.prefs and cat in OUTDOOR:
        return None  # просили под крышу
    if "quiet" in ctx.prefs and cat in ("торговый центр", "бар", "ночной клуб", "фастфуд"):
        return None  # просили тихо — не ведём в шумные места
    if "unusual" in ctx.prefs and cat in ("квест", "океанариум", "арт-центр", "развлечения"):
        s += 1.0
    if ctx.i.get("dinner") and cat == "фастфуд":
        s -= 1.5
    if ctx.i.get("kazakh_food") and cat in ("ресторан", "кафе", "фастфуд"):
        attrs = " ".join((p.get("dgis") or {}).get("attrs") or []).lower()
        if "kazakh" in tags.get("cuisine", "") or "казах" in attrs or "национальн" in attrs:
            s += 2.5  # просили национальную кухню — место, где она точно есть
        elif re.search(r"дастархан|казан|бешбармак|qazaq|kazakh|nomad|кочевник|юрт|тайказан|кымыз|баурсак|ұлттық|тандыр|алаша|"
                       r"асхана|tary|тары|шанырак|шаңырақ|салтанат|sandyq|жеті|regional|local", p["name"].lower() + " " + tags.get("cuisine", "")):
            s += 1.5  # по названию похоже на национальную кухню (сигнал слабее 2GIS, ИИ не утверждает)
        else:
            s -= 1.2  # просили национальную — суши, пицца и бургеры ниже
    if (ctx.i.get("group_size") or 0) >= 5 and cat in ("ресторан", "кафе", "бар"):
        attrs = " ".join((p.get("dgis") or {}).get("attrs") or []).lower()
        seats = [int(x) for x in re.findall(r"до (\d+)", attrs)]
        if "заказ столиков" in attrs or (seats and max(seats) >= 40):
            s += 1.2  # компания: место, где можно забронировать стол и хватит места
        elif cat == "кафе" and not seats:
            s -= 0.5  # маленькая кофейня на 5+ человек — скорее тесно
    if "kids" in ctx.prefs and cat == "детский центр":
        s += 1.0  # игровой центр для ребёнка лучше, чем «просто ТРЦ»
    if "food" in ctx.prefs and cat == "ресторан" and ("romantic" in ctx.prefs or (arrive and arrive.hour >= 18)):
        s += 1.2  # ужин — это ресторан, а не кофейня
    s += min(p.get("richness", 0), 10) * 0.08
    sightseeing = not ctx.prefs or bool(ctx.prefs & {"sights", "culture", "walk", "nature"})
    if "wikidata" in tags:
        s += 0.8 if sightseeing else 0.2
    if p.get("wiki") and sightseeing:
        s += min(p["wiki"]["sitelinks"], 30) * 0.03  # статьи на 30 языках — +0,9: символ города важнее безымянного сквера
    if "kids" in ctx.prefs and cat in ("детская площадка", "детский центр", "развлечения", "батутный парк", "океанариум", "зоопарк",
                                       "парк развлечений", "каток", "аквапарк"):
        s += 1.2  # именно детские места важнее «просто ТРЦ»
    if p.get("unnamed"):
        s -= 0.6  # безымянная — честно, но менее интересна
    if "work" in ctx.prefs and tags.get("internet_access") in ("wlan", "yes", "wifi"):
        s += 1.0
    if ctx.i.get("mobility") == "limited":
        wc = tags.get("wheelchair")
        if wc == "no":
            return None
        s += 0.8 if wc == "yes" else (0.3 if wc == "limited" else -0.4)
    outdoor = is_outdoor(p)
    if outdoor and ctx.cold == "severe":
        return None
    if outdoor and ctx.cold == "cold":
        s -= 0.8
    if not outdoor and ctx.cold != "ok":
        s += 0.4
    if arrive is not None and (arrive.hour >= 23 or arrive.hour < 6):
        if cat in ("парк", "сад", "детская площадка", "фонтан"):
            return None  # глубокая ночь: в сквер не зовём, фонтаны выключены
    if arrive is not None and outdoor and ctx.is_dark(arrive):
        if cat in ("детская площадка", "сад", "парк"):
            if "kids" in ctx.prefs or cat == "детская площадка" or not p.get("wiki"):
                return None  # в темноте — только известные центральные парки
            s -= 0.8
    if tags.get("brand"):
        s -= 0.15  # чуть выше местные места, чем сети
    return s


def stay_of(p: dict, ctx: "Ctx") -> int:
    if p["id"] in getattr(ctx, "bonus", {}):
        return ctx.bonus[p["id"]]   # продлённое время: свободный запас отдали месту, а не «вернуться раньше»
    if ctx.work and p["category"] in ("кафе", "коворкинг", "библиотека"):
        return ctx.work_stay
    if getattr(ctx, "short_explicit", False) and p["category"] in ctx.explicit:
        return 10
    if getattr(ctx, "quick_food", False) and p["category"] in ("кафе", "ресторан", "фастфуд"):
        return 12   # окно впритык: быстро перекусить, а не обед на 50 минут
    if p["category"] in ("парк", "сад") and p["category"] in getattr(ctx, "explicit", ()):
        w = getattr(ctx, "window", 60) or 60
        return max(15, min(45, int(w * 0.3) // 5 * 5))  # «погулять в парке» — парк и есть цель прогулки
    if "walk" in ctx.prefs and p["category"] in ("парк", "сад", "фонтан", "памятник", "достопримечательность", "смотровая"):
        return 10  # прогулка: место — точка маршрута, а не цель
    st = STAY.get(p["category"], 20)
    w = getattr(ctx, "window", None)
    if w and w <= 45 and p["category"] not in LONG_STAYS:
        st = max(10, min(st, int(w * 0.5) // 5 * 5))  # короткое окно — короткая остановка, а не отказ
    elif w and w >= 200 and p["category"] not in LONG_STAYS:
        # «весь день»: не бегом — в музее, парке, ресторане можно побыть дольше
        st = int(min(st * min(2.0, w / 150), st + 60)) // 5 * 5
    return st


def _leg(a, b, t: datetime, ctx: Ctx) -> dict:
    walk = geo.walk_min_est(a, b) * ctx.pace
    if walk <= ctx.max_leg or ctx.walk_only:
        return {"mode": "walk", "min": walk, "outdoor": walk}
    car = geo.car_min_est(a, b, t) + geo.TAXI_WAIT_MIN
    if car + 3 >= walk:  # такси не быстрее — идём пешком
        return {"mode": "walk", "min": walk, "outdoor": walk}
    return {"mode": "taxi", "min": car, "outdoor": 2}


def simulate(seq: list[dict], A, B, start: datetime, deadline: datetime, ctx: Ctx):
    """Быстрая оценка плана (без внешних запросов). None — если план не годится."""
    malls = [p for p in seq if p["category"] == "торговый центр"]
    if malls and any(q.get("inside") and geo.haversine_km((q["lat"], q["lon"]), (m["lat"], m["lon"])) < 0.25
                     for q in seq for m in malls):
        return None  # «ТРЦ» и «кафе внутри этого ТРЦ» — одна остановка, а не две
    meals = sum(1 for p in seq if p["category"] in ("ресторан", "фастфуд"))
    cafes = sum(1 for p in seq if p["category"] == "кафе")
    long_day = (deadline - start).total_seconds() > 240 * 60
    both_asked = "coffee" in ctx.prefs and "food" in ctx.prefs
    if meals > 1 or cafes > 1 or (meals and cafes and not (ctx.work or long_day or both_asked)):
        return None  # «перекусить» — одно место поесть, а не пельменная и кафе подряд; кофе + обед — только если просили оба или целый день
    t = start
    pos = A
    legs, stops = [], []
    score = 0.0
    cats = set()
    for p in seq:
        pt = (p["lat"], p["lon"])
        if stops and geo.haversine_km(pos, pt) < 0.08:
            return None  # две остановки в одной точке — это одно место, а не маршрут
        if p.get("unnamed") and any(q.get("unnamed") for q, *_ in stops):
            return None  # две безымянные площадки подряд — скучно и непонятно
        lg = _leg(pos, pt, t, ctx)
        t += timedelta(minutes=lg["min"])
        stay = stay_of(p, ctx)
        arrive, leave = t, t + timedelta(minutes=stay)
        st, conf, _ = interval_status(p.get("tags"), p["category"], arrive, leave)
        if st in ("closed", "closes_soon"):
            return None
        sc = stop_score(p, ctx, arrive)
        if sc is None:
            return None
        if st == "unknown":
            sc -= 0.6
        elif conf == "verified":
            sc += 0.5
        if p["category"] in cats:
            sc -= 1.6  # разнообразие: две площадки или два бара подряд скучнее, чем разные места
        cats.add(p["category"])
        for ref in (A, B):  # остановка прямо у старта/финиша — мало пользы
            if geo.haversine_km(pt, ref) < 0.2:
                sc -= 0.7
        score += sc
        legs.append(lg)
        stops.append((p, arrive, leave, stay))
        t = leave
        pos = pt
    lg = _leg(pos, B, t, ctx)
    legs.append(lg)
    t += timedelta(minutes=lg["min"])
    window = (deadline - start).total_seconds() / 60
    # запас на то, что реальный путь по улицам длиннее оценки; на коротком окне (20 мин) — поменьше, иначе вечный отказ
    buffer = max(4.0 if window < 30 else 7.0, window * 0.09)
    slack = (deadline - t).total_seconds() / 60
    if slack < buffer:
        return None
    used = window - slack
    outdoor = sum(x["outdoor"] for x in legs)
    taxis = sum(1 for x in legs if x["mode"] == "taxi")
    cold_mult = {"ok": 0.01, "cold": 0.04, "severe": 0.1}[ctx.cold]
    if "walk" in ctx.prefs and ctx.cold == "ok":
        cold_mult = -0.04  # человек хочет именно пройтись — ходьба в плюс, а не в минус
    score -= outdoor * cold_mult + taxis * 0.9 + abs(used / window - 0.88) * 2.0
    score += 0.35 * len(seq)
    for pr in ("food", "coffee"):
        if pr in ctx.prefs and getattr(ctx, "window", 90) >= 60 and not any(p["category"] in PREF_CATS[pr] for p in seq):
            score -= 2.0  # «музеи и поесть» — без еды план не полный
    for c in ctx.explicit:
        if sum(1 for p in seq if p["category"] == c) > 1:
            score -= 3.0  # «хочу в библиотеку» — одна библиотека, а не две подряд
    if ctx.explicit and not any(p["category"] in ctx.explicit for p in seq):
        score -= 5.0  # «хочу в музей» — план без музея только если музей никак не помещается
    return {"score": score, "finish": t, "legs": legs, "stops": stops, "slack": slack}


def _nm(s: str) -> str:
    return re.sub(r"[^a-zа-яёәіңғүұқөһ0-9]", "", (s or "").lower().split("(")[0])


def _same_place(p: dict, label: str) -> bool:
    """«Хан Шатыр» как остановка, когда человек и так в Хан Шатыре, — бессмысленно."""
    a, b = _nm(p["name"]), _nm(label)
    return len(a) >= 4 and len(b) >= 4 and (a in b or b in a)


try:
    IMAGES = json.loads((ROOT / "data" / "images.json").read_text(encoding="utf-8"))
except Exception:
    IMAGES = {}


def image_of(p: dict) -> str | None:
    """Фото места: из Википедии, иначе — главное изображение Wikidata по тегу OSM (Wikimedia Commons)."""
    return (p.get("wiki") or {}).get("image") or IMAGES.get((p.get("tags") or {}).get("wikidata") or "")


def contacts(t: dict) -> dict:
    """Базовые контакты из OpenStreetMap (кейс просит «время работы / базовые контактные данные, если есть»)."""
    phone = (t.get("contact:phone") or t.get("phone") or "").split(";")[0].strip() or None
    site = t.get("contact:website") or t.get("website")
    ig = t.get("contact:instagram")
    if not site and ig:
        site = ig if ig.startswith("http") else "https://instagram.com/" + ig.lstrip("@").split("/")[-1]
    return {"phone": phone, "website": site}


def _why_not(p, A, start, ctx):
    """Причина, по которой подходящее по типу место не попало в маршрут — считаем для прозрачности."""
    if p["category"] not in ctx.wanted or p["id"] in ctx.rejected_seen:
        return
    ctx.rejected_seen.add(p["id"])   # второй проход поиска не должен считать то же место дважды
    walk = geo.walk_min_est(A, (p["lat"], p["lon"])) * ctx.pace
    move = walk if walk <= ctx.max_leg else geo.car_min_est(A, (p["lat"], p["lon"]), start) + geo.TAXI_WAIT_MIN
    arrive = start + timedelta(minutes=move)
    extra = ""
    if stop_score(p, ctx, arrive) is None:
        key = "мороз, темнота или не то, что просили"
    else:
        st, conf, txt = interval_status(p.get("tags"), p["category"], arrive, arrive + timedelta(minutes=stay_of(p, ctx)))
        if st in ("closed", "closes_soon"):
            key = "закрыто к приходу или закроется во время визита"
            extra = txt.split(" · ")[0].strip() if conf == "verified" else ""
        else:
            key = "не успеть туда и к сроку"
            extra = f"{geo.haversine_km(A, (p['lat'], p['lon'])):.1f} км".replace(".", ",")
    ctx.rejected[key] = ctx.rejected.get(key, 0) + 1
    # примеры для «Почему не другие места» — самые известные из отсеянных (Википедия / отзывы 2GIS)
    if not p.get("unnamed") and not p["name"].lower().startswith("детская площадка") and p["name"].strip().lower() not in GENERIC             and len(p["name"]) > 3:
        fame = ((p.get("wiki") or {}).get("sitelinks") or 0) * 50 + ((p.get("dgis") or {}).get("reviews") or 0)
        ex = ctx.rejected_ex.setdefault(key, [])
        label = p["name"] + (f" ({extra})" if extra else "")
        if all(n != label for _, n in ex):
            ex.append((fame, label))
        ex.sort(key=lambda x: -x[0])
        del ex[2:]


def _candidates(A, B, start, deadline, ctx: Ctx, exclude: set, required: set):
    window = (deadline - start).total_seconds() / 60
    loop = geo.haversine_km(A, B) < 0.3
    o_label = (ctx.i.get("origin") or {}).get("label", "")
    d_label = (ctx.i.get("destination") or {}).get("label", "")
    # «где поесть / кофе / с ребёнком» у ЭКСПО — кафе в двух минутах и есть лучший ответ; «рядом с началом не предлагаем»
    # нужно только для прогулки, чтобы она не кончалась у порога
    errand = bool(ctx.prefs & {"food", "coffee", "work", "kids", "night", "shopping"})
    out = []
    for p in POIS:
        if p["id"] in exclude:
            continue
        pt0 = (p["lat"], p["lon"])
        if p["id"] not in required and (_same_place(p, o_label) or (not loop and _same_place(p, d_label))):
            continue
        if loop and p["id"] not in required and not (errand and geo.haversine_km(pt0, A) >= 0.03):
            # прогулка «и обратно»: кафе внутри своего же ТРЦ — отличный вариант на короткое окно
            if geo.haversine_km(pt0, A) < 0.06 and not p.get("inside"):
                continue
            if not (p.get("inside") and geo.haversine_km(pt0, A) < 0.3) and geo.haversine_km(pt0, B) < 0.2:
                continue
        if p["id"] not in required and p.get("inside") and geo.haversine_km(A, B) >= 0.3 and geo.haversine_km(pt0, B) < 0.4:
            continue  # кафе внутри ТРЦ-финиша — это уже «на месте», а не остановка по пути
        # остановка «по пути» не может быть самим финишем или стартом
        if not loop and p["id"] not in required and (geo.haversine_km(pt0, B) < 0.25 or geo.haversine_km(pt0, A) < 0.15):
            continue
        if p["category"] in LONG_STAYS and window < STAY[p["category"]] + 30 and p["id"] not in required:
            continue
        pt = (p["lat"], p["lon"])
        # быстрый отсев по геометрии: даже на такси должно хватать времени
        sim = simulate([p], A, B, start, deadline, ctx)
        if sim is None and p["id"] not in required:
            _why_not(p, A, start, ctx)  # для «Как посчитано»: сколько мест отсеяно и почему
            continue
        sc = stop_score(p, ctx) or 0
        detour = geo.haversine_km(A, pt) + geo.haversine_km(pt, B) - geo.haversine_km(A, B)
        far_w = 1.2 if ctx.i.get("mobility") == "limited" else 0.35   # «не далеко»: ближнее место важнее красивого дальнего
        out.append((sc - detour * far_w, p))
    out.sort(key=lambda x: -x[0])
    picked, per_cat = [], {}
    for sc, p in out:  # разнообразие: не больше 7 мест одной категории в переборе
        c = p["category"]
        if per_cat.get(c, 0) >= 7 and p["id"] not in required:
            continue
        per_cat[c] = per_cat.get(c, 0) + 1
        picked.append(p)
        if len(picked) >= 28:
            break
    for rid in required:
        if not any(p["id"] == rid for p in picked):
            picked += [p for p in POIS if p["id"] == rid]
    return picked


def _projection(A, B, p) -> float:
    ax, ay = A[1], A[0]
    bx, by = B[1] - ax, B[0] - ay
    px, py = p["lon"] - ax, p["lat"] - ay
    L = bx * bx + by * by
    return (px * bx + py * by) / L if L else 0


PRUNE = True   # отсечение троек по парам (выключается для сравнения в тестах)


# предел времени на перебор троек: на слабом сервере (бесплатный хостинг) длинное окно перебиралось бы десятки секунд.
# Кандидаты отсортированы от лучших, поэтому ранние тройки — самые сильные; одиночки и пары перебираются всегда целиком.
PLAN_BUDGET = float(os.getenv("PLAN_BUDGET", "2.5"))


def search(A, B, start, deadline, ctx: Ctx, exclude=frozenset(), required=frozenset(), max_stops=3):
    cands = _candidates(A, B, start, deadline, ctx, set(exclude), set(required))
    t_begin = time.time()
    loop = geo.haversine_km(A, B) < 0.3
    results = []
    ok_pairs: set[frozenset] = set()   # пары, которые помещаются хотя бы в одном порядке
    for k in range(1, max_stops + 1):
        for combo in itertools.combinations(cands, k):
            if required and not set(required) <= {p["id"] for p in combo}:
                continue
            # если тройка помещается, то и любая её пара помещается (без одного места времени только больше) —
            # тройки с «невозможной» парой не перебираем: в разы быстрее на длинных окнах, результат тот же
            if k == 3 and time.time() - t_begin > PLAN_BUDGET:
                ctx.cut = True
                break
            if PRUNE and k == 3 and not required and not all(frozenset((a["id"], b["id"])) in ok_pairs for a, b in itertools.combinations(combo, 2)):
                continue
            if loop:
                orders = itertools.permutations(combo) if k > 1 else [combo]
            else:
                orders = [sorted(combo, key=lambda p: _projection(A, B, p))]
            for seq in orders:
                sim = simulate(list(seq), A, B, start, deadline, ctx)
                if sim:
                    results.append((sim["score"], list(seq), sim))
                    if k == 2:
                        ok_pairs.add(frozenset(p["id"] for p in seq))
    results.sort(key=lambda x: -x[0])
    return results, len(cands)


def refine(seq, A, B, start, deadline, ctx: Ctx):
    """Пересчёт лучшего плана по реальным маршрутам (улицы, а не прямая)."""
    t = start
    pos = A
    steps = [{"type": "start", "time": _fmt(t)}]
    outdoor, walk_km, max_leg_out, taxis, estimated = 0.0, 0.0, 0.0, 0, False
    points = [(p["lat"], p["lon"]) for p in seq] + [B]
    for i, pt in enumerate(points):
        lg = _leg(pos, pt, t, ctx)
        if lg["mode"] == "walk":
            r = geo.route(pos, pt, "foot", fallback=True)
            if r:
                r = {**r, "min": r["min"] * ctx.pace}
            if r and r["min"] > ctx.max_leg and not ctx.walk_only and (r["min"] > geo.walk_min_est(pos, pt) * ctx.pace * 1.25
                                                                      or ctx.i.get("mobility") == "limited"):
                lg = {"mode": "taxi"}  # реальный путь оказался сильно длиннее (река, развязки) — подвезём
            elif r:
                leg = {"type": "leg", "mode": "walk", "min": round(r["min"]), "km": round(r["km"], 2),
                       "coords": r["coords"], "estimated": False}
            else:
                m = geo.walk_min_est(pos, pt) * ctx.pace
                leg = {"type": "leg", "mode": "walk", "min": round(m), "km": round(m / ctx.pace / 60 * geo.WALK_KMH, 2),
                       "coords": [[pos[1], pos[0]], [pt[1], pt[0]]], "estimated": True}
                estimated = True
        if lg["mode"] == "taxi":
            r = geo.route(pos, pt, "car", fallback=True)
            f, label = geo.traffic_factor(t)
            drive = (r["min"] if r else geo.car_min_est(pos, pt, t) / f) * f
            jam = dgis.drive(pos, pt, t, ctx.real_now)
            if jam:
                drive, label = jam[0], jam[1]
            leg = {"type": "leg", "mode": "taxi", "min": round(drive + geo.TAXI_WAIT_MIN), "drive_min": round(drive),
                   "wait_min": geo.TAXI_WAIT_MIN, "km": round(r["km"] if r else geo.haversine_km(pos, pt) * 1.4, 2),
                   "coords": r["coords"] if r else [[pos[1], pos[0]], [pt[1], pt[0]]],
                   "traffic": label, "estimated": True}
            taxis += 1
        if leg["mode"] == "walk":
            outdoor += leg["min"]
            walk_km += leg["km"]
            max_leg_out = max(max_leg_out, leg["min"])
        else:
            outdoor += 2   # дойти до машины и от неё — честно, а не «0 минут на улице»
            max_leg_out = max(max_leg_out, 2)
        steps.append(leg)
        t += timedelta(minutes=leg["min"])
        if i < len(seq):
            p = seq[i]
            stay = stay_of(p, ctx)
            arrive, leave = t, t + timedelta(minutes=stay)
            st, conf, text = interval_status(p.get("tags"), p["category"], arrive, leave)
            if st in ("closed", "closes_soon"):
                return None
            steps.append({"type": "stop", "id": p["id"], "name": p["name"], "name_en": p.get("name_en"), "name_kk": p.get("name_kk"),
                          "category": p["category"],
                          "lat": p["lat"], "lon": p["lon"], "arrive": _fmt(arrive), "leave": _fmt(leave), "stay": stay,
                          "hours": {"status": st, "confidence": conf, "source": p.get("hours_src", "OSM"),
                                    "text": text + (" · по 2GIS" if p.get("hours_src") == "2GIS" else "")},
                          "rating": (p.get("dgis") or {}).get("rating"), "reviews": (p.get("dgis") or {}).get("reviews"),
                          "dgis_id": (p.get("dgis") or {}).get("id"),
                          "wiki_url": (p.get("wiki") or {}).get("article"), "image": image_of(p),
                          "wheelchair": (p.get("tags") or {}).get("wheelchair"),
                          **contacts(p.get("tags") or {}),
                          "facts": _facts(p) | ({"по_пути": f"крюк всего {d} мин" if d >= 2 else "прямо по пути, без крюка"} if (d := _detour_min(A, B, p)) <= 8
                                                and geo.haversine_km(A, B) >= 0.3 else {}) | {"остановка": f"{i + 1} из {len(seq)}",
                                                "в_помещении": not is_outdoor(p)},
                          "source": p["source"]})
            t = leave
        pos = pt
    slack = (deadline - t).total_seconds() / 60
    if slack < 3:
        return None
    steps.append({"type": "end", "time": _fmt(t)})
    return {"steps": steps, "finish": t, "slack": round(slack), "outdoor": round(outdoor), "walk_km": round(walk_km, 1),
            "max_outdoor_leg": round(max_leg_out), "taxis": taxis, "estimated": estimated}


def _detour_min(A, B, p) -> int:
    pt = (p["lat"], p["lon"])
    if geo.haversine_km(A, B) < 0.3:
        return round(geo.walk_min_est(A, pt))
    return max(0, round((geo.haversine_km(A, pt) + geo.haversine_km(pt, B) - geo.haversine_km(A, B)) * geo.DETOUR / geo.WALK_KMH * 60))


def _facts(p: dict) -> dict:
    t = p.get("tags", {})
    keep = ("cuisine", "outdoor_seating", "internet_access", "wheelchair", "description", "description:ru",
            "website", "contact:instagram", "fee", "brand")
    out = {k: t[k] for k in keep if k in t}
    if p.get("inside"):
        out["внутри"] = f"внутри ТРЦ «{p['inside']}»" + (" (часы — по ТРЦ)" if p.get("hours_from_mall") else "")
    if p.get("wiki"):
        w = p["wiki"]
        if w["sitelinks"] >= 3:
            # без «на 29 языках» — ИИ превращал это в корявые фразы про статьи; смысл — насколько место известное
            out["известность"] = ("одно из самых известных мест Астаны" if w["sitelinks"] >= 15
                                  else "известное место города (есть в Википедии)")
        if w.get("extract"):
            out["википедия"] = w["extract"]
    elif "wikidata" in t or "wikipedia" in t:
        out["известность"] = "есть статья в Википедии — известное место города"
    if p["name"] in VIEW_POINTS:
        out["смотровая"] = "смотровая площадка на высоте 97 м (Википедия)"
    d = p.get("dgis")
    if d:
        if d.get("rating") and d.get("reviews"):
            out["рейтинг_2гис"] = f"{d['rating']} ({d['reviews']} оценок)"
        if d.get("attrs"):
            out["отметки_2гис"] = d["attrs"]
    return out


def _dgis_bonus(p: dict, ctx: Ctx) -> float:
    d = p.get("dgis")
    if not d:
        return 0.0
    b = 0.0
    if d.get("rating") and (d.get("reviews") or 0) >= 20:
        b += max(-1.0, min(0.6, (d["rating"] - 4.3) * 1.2))
    attrs = " ".join(d.get("attrs") or []).lower()
    if ctx.work and "ноутбук" in attrs:
        b += 1.5
    if "kids" in ctx.prefs and "детск" in attrs:
        b += 0.8
    if ctx.i.get("language") == "en" and "английск" in attrs:
        b += 0.4
    return b


def _enrich(results: list, ctx: Ctx) -> None:
    """Уточняем кандидатов по 2GIS (часы, рейтинг, «можно с ноутбуком») и пересортировываем варианты."""
    if not dgis.KEY:
        return
    uniq = {p["id"]: p for _, seq, _ in results for p in seq}
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(dgis.apply, uniq.values()))
    dgis.save()
    results.sort(key=lambda r: -(r[0] + sum(_dgis_bonus(p, ctx) for p in r[1])))


def _prefetch(results, A, B):
    """Заранее и параллельно запрашиваем реальные маршруты для лучших планов — ответ в разы быстрее."""
    jobs = set()
    for _, seq, sim in results:
        pts = [A] + [(p["lat"], p["lon"]) for p in seq] + [B]
        for (a, b), lg in zip(zip(pts, pts[1:]), sim["legs"]):
            jobs.add((a, b, "foot"))
            if lg["mode"] == "taxi":
                jobs.add((a, b, "car"))
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda j: geo.route(*j), jobs))


def _nearest_explicit(A, start, ctx) -> str | None:
    """«Хочу в боулинг», а не успеть: назвать ближайший и сколько до него ехать — это полезнее, чем «нет мест»."""
    best = None
    for p in POIS:
        if p["category"] in ctx.explicit:
            d = geo.haversine_km(A, (p["lat"], p["lon"]))
            if best is None or d < best[0]:
                best = (d, p)
    if not best:
        return None
    d, p = best
    taxi = round(geo.car_min_est(A, (p["lat"], p["lon"]), start) + geo.TAXI_WAIT_MIN)
    need = 2 * taxi + STAY.get(p["category"], 30)
    return (f"Ближайшее место «{p['category']}» — {p['name']}, {d:.1f} км, на такси ~{taxi} мин в одну сторону (оценка). "
            f"Туда, {STAY.get(p['category'], 30)} мин там и обратно — около {need} мин: в это окно не помещается. "
            f"Напиши, сколько у тебя времени, — например «есть 3 часа».").replace(".0 км", " км")


def _nearest_of(cats: set, A, start, ctx):
    """Ближайшее место нужного типа, открытое к приходу: (место, мин пешком, сколько нужно на «туда-побыть-обратно»)."""
    best = None
    for p in POIS:
        if p["category"] not in cats or p.get("unnamed"):
            continue
        d = geo.haversine_km(A, (p["lat"], p["lon"]))
        if d > 3 or (best and d >= best[0]):
            continue
        walk = geo.walk_min_est(A, (p["lat"], p["lon"])) * ctx.pace
        arrive = start + timedelta(minutes=walk)
        if interval_status(p.get("tags"), p["category"], arrive, arrive + timedelta(minutes=stay_of(p, ctx)))[0] == "open":
            best = (d, p, walk)
    if not best:
        return None
    _, p, walk = best
    return p, walk, 2 * walk + stay_of(p, ctx)


def _glance(A, ctx) -> str | None:
    """Совсем короткое окно, хочется «что-то посмотреть»: одно известное место на улице, куда успеешь дойти и вернуться."""
    window = getattr(ctx, "window", 0) or 0
    best = None
    for p in POIS:
        if p["category"] not in ("памятник", "достопримечательность", "фонтан", "смотровая") or p.get("unnamed"):
            continue
        if p["category"] == "фонтан" and not 5 <= ctx.now.month <= 9:
            continue   # фонтаны работают с мая по сентябрь
        d = geo.haversine_km(A, (p["lat"], p["lon"]))
        if d > 0.8 or d < 0.12:   # ближе 120 м — ты уже там, это не совет
            continue
        walk = geo.walk_min_est(A, (p["lat"], p["lon"])) * ctx.pace
        if 2 * walk + 4 > window:
            continue
        fame = ((p.get("wiki") or {}).get("sitelinks") or 0) + (3 if p.get("wiki") else 0)
        if best is None or (fame, -walk) > (best[0], -best[1]):
            best = (fame, walk, p)
    if not best:
        return None
    _, walk, p = best
    ctx.tip = {"name": p["name"], "lat": p["lat"], "lon": p["lon"], "walk_min": round(walk), "category": p["category"]}
    return (f"Успеешь одно: {p['name']} — {round(walk)} мин пешком. Посмотри пару минут снаружи и возвращайся — "
            f"туда-обратно около {round(2 * walk + 3)} мин из {round(window)}.")


def _grab_and_go(A, start, ctx, cats=None, max_km=0.6) -> str | None:
    """Совсем короткое окно, но человек хочет поесть/кофе: ближайшее открытое место — «возьми с собой»."""
    cats = set(cats or ())
    for pr in ("food", "coffee"):
        if pr in ctx.prefs:
            cats |= PREF_CATS[pr]
    if not cats:
        return _glance(A, ctx)
    window = getattr(ctx, "window", 0) or 0
    best = None
    for p in POIS:
        if p["category"] not in cats:
            continue
        d = geo.haversine_km(A, (p["lat"], p["lon"]))
        if d > max_km:
            continue
        walk = geo.walk_min_est(A, (p["lat"], p["lon"])) * ctx.pace
        if window and 2 * walk + 6 > window:
            continue
        st, conf, _ = interval_status(p.get("tags"), p["category"], start + timedelta(minutes=walk), start + timedelta(minutes=walk + 5))
        if st == "open" and (best is None or walk < best[0]):
            best = (walk, p, conf)
    if not best:
        return None
    walk, p, conf = best
    ctx.tip = {"name": p["name"], "lat": p["lat"], "lon": p["lon"], "walk_min": round(walk), "category": p["category"]}
    hours = "" if conf == "verified" else " (часы — оценка)"
    return (f"Посидеть не успеешь, но можно взять с собой: {p['name']} — {round(walk)} мин пешком{hours}. "
            "Туда, заказ навынос и обратно как раз помещается в окно.")


def _cafe_at(B, arrive, spare, ctx) -> str | None:
    """До цели остаётся немного свободного времени, а цель — ТРЦ или рядом с кафе: «иди сразу туда, посиди в X»."""
    if spare < 12:
        return None
    best = None
    for p in POIS:
        if p["category"] not in ("кафе", "фастфуд") or p.get("unnamed"):
            continue
        d = geo.haversine_km(B, (p["lat"], p["lon"]))
        if d > 0.15:
            continue
        st, conf, _ = interval_status(p.get("tags"), p["category"], arrive, arrive + timedelta(minutes=10))
        if st == "open" and (best is None or d < best[0]):
            best = (d, p)
    if not best:
        return None
    p = best[1]
    ctx.tip = {"name": p["name"], "lat": p["lat"], "lon": p["lon"], "walk_min": 0, "category": p["category"]}
    return (f"Отдельную остановку по пути не успеть, но можно сразу идти к цели: рядом с ней {p['name']} — "
            f"посиди там ~{int(spare - 5)} мин до начала.")


def _no_plan_advice(A, B, start, window, direct_walk, ctx) -> str:
    """Честный и полезный отказ: почему не вышло и что делать вместо этого."""
    if geo.haversine_km(A, B) < 0.3:
        if window < 25:
            grab = _grab_and_go(A, start, ctx)
            if grab:
                return grab
            return f"Окно {round(window)} мин — на остановку с возвращением не хватает. Можно просто выйти подышать на 10 минут."
        near = _nearest_explicit(A, start, ctx)
        if near:
            return near
        # посидеть не выходит — но посмотреть одно место снаружи или взять кофе с собой обычно успеваешь
        warm = "indoor" in ctx.prefs or ctx.cold != "ok" or ctx.i.get("mobility") == "limited"
        quick = (None if warm else _glance(A, ctx)) or _grab_and_go(A, start, ctx, {"кафе"}, 0.6 if warm else 1.0)
        if quick:
            return quick
        return ("Рядом нет подходящих мест, до которых можно дойти и вернуться за это время"
                + (" с учётом твоих пожеланий" if ctx.prefs else "") + ". Попробуй убрать пожелания или увеличить окно.")
    walk = round(direct_walk["min"]) if direct_walk else round(geo.walk_min_est(A, B))
    taxi = round(geo.car_min_est(A, B, start) + geo.TAXI_WAIT_MIN)
    inside = _cafe_at(B, start + timedelta(minutes=min(walk, taxi)), window - min(walk, taxi), ctx)
    if inside:
        return inside
    if walk <= window:
        return f"На остановки времени нет: пешком до цели {walk} мин из {round(window)}. Просто иди — успеешь с запасом {round(window) - walk} мин."
    if taxi <= window:
        return f"Пешком не успеть ({walk} мин при окне {round(window)}). На такси ~{taxi} мин с подачей и пробками (оценка) — лучше ехать сразу."
    return f"Не успеть даже на такси: ~{taxi} мин (оценка), а окно {round(window)} мин. Стоит предупредить, что опоздаешь."


VIA_STAY = {"торговый центр": 60, "университет": 45, "музей": 60, "кафе": 30, "ресторан": 60, "парк": 30}


def plan_chain(intent: dict, now: datetime, w: dict, has_deadline: bool = False, hard_deadline=None) -> dict:
    """Человек сам перечислил, куда зайти по порядку («в НУ, потом в Мегу, в конце домой»): ничего не подбираем,
    а считаем этот маршрут честно — время пешком/на такси с пробками, сколько побыть в каждом пункте, во сколько будешь дома."""
    ctx = Ctx(intent, now, w)
    A = (intent["origin"]["lat"], intent["origin"]["lon"])
    B = (intent["destination"]["lat"], intent["destination"]["lon"])
    deadline = datetime.fromisoformat(intent["deadline"]) if has_deadline else now + timedelta(hours=14)
    ctx.window = (deadline - now).total_seconds() / 60
    seq, ctx.bonus = [], {}
    for i, v in enumerate(intent["via"]):
        pt_v = (v["lat"], v["lon"])
        big = [p for p in POIS if p["category"] in ("торговый центр", "музей", "театр", "кино")
               and geo.haversine_km(pt_v, (p["lat"], p["lon"])) < 0.25]
        near = min(big or POIS, key=lambda p: geo.haversine_km(pt_v, (p["lat"], p["lon"])))   # ТРЦ важнее кафе внутри него
        same = geo.haversine_km(pt_v, (near["lat"], near["lon"])) < (0.25 if big else 0.15)
        stems = {w[:5] for w in re.findall(r"\w{4,}", v["label"].lower())}
        if same and near["category"] != "торговый центр" and not stems & {w[:5] for w in re.findall(r"\w{4,}", near["name"].lower())}:
            same = False   # рядом другое здание — его часы к твоему пункту не относятся
        pid = f"via/{i}"
        cat = near["category"] if same else "пункт"
        stay = v.get("stay") or VIA_STAY.get(cat, 30)
        seq.append({"id": pid, "name": v["label"], "category": cat, "lat": v["lat"], "lon": v["lon"],
                    "tags": dict(near.get("tags") or {}) if same else {"opening_hours": "24/7"},
                    "dgis": near.get("dgis") if same else None, "wiki": near.get("wiki") if same else None,
                    "source": near["source"] if same else "твой пункт маршрута"})
        ctx.bonus[pid] = int(stay)
    best = refine(seq, A, B, now, deadline, ctx)
    if not best:
        # не успеваешь к сроку или место закрыто — честно говорим, что именно, и всё равно показываем, сколько займёт
        relaxed = refine([{**p, "tags": {"opening_hours": "24/7"}} for p in seq], A, B, now, now + timedelta(hours=20), ctx)
        if not relaxed:
            return {"error": "Не получилось посчитать этот маршрут — проверь названия пунктов."}
        fin = relaxed["finish"]
        if has_deadline and hard_deadline and fin <= hard_deadline and all(
                s["hours"]["status"] == "open" for s in refine(seq, A, B, now, now + timedelta(hours=20), ctx)["steps"] if s["type"] == "stop"):
            best = relaxed   # к сроку успеваешь, но без запаса на вход — показываем маршрут и честно предупреждаем
            best["_tight"] = True
    dropped = []
    if not best and not (has_deadline and relaxed["finish"] > deadline):
        # по очереди: место ещё не открылось — подождать (подольше в предыдущем пункте); закрыто совсем — убрать
        for _ in range(len(seq) + 2):
            loose = refine([{**p, "tags": {"opening_hours": "24/7"}} for p in seq], A, B, now, now + timedelta(hours=20), ctx)
            if not loose:
                break
            bad = None
            for p, s in zip(seq, [s for s in loose["steps"] if s["type"] == "stop"]):
                day = now.replace(hour=0, minute=0)
                a = day + timedelta(hours=int(s["arrive"][:2]), minutes=int(s["arrive"][3:]))
                a += timedelta(days=1) if a < now - timedelta(minutes=1) else timedelta(0)
                st, conf, text = interval_status(p.get("tags"), p["category"], a, a + timedelta(minutes=s["stay"]))
                if st in ("closed", "closes_soon"):
                    bad = (p, a, st, text)
                    break
            if not bad:
                break
            p, a, st, text = bad
            k = seq.index(p)
            opens = next_open(p.get("tags"), p["category"], a) if st == "closed" else None
            if opens and k > 0:
                wait = int((opens - a).total_seconds() // 60) + 2
                prev = seq[k - 1]
                ctx.bonus[prev["id"]] += wait
                dropped.append(f"«{p['name']}» открывается в {opens:%H:%M} — побудешь в «{prev['name']}» на {wait} мин дольше")
            elif opens and k == 0 and (opens - now) <= timedelta(hours=2):
                wait = int((opens - a).total_seconds() // 60) + 2
                now = now + timedelta(minutes=wait)
                dropped.append(f"«{p['name']}» открывается в {opens:%H:%M} — выходи на {wait} мин позже")
            else:
                seq.remove(p)
                dropped.append(f"{p['name']}: {text} — убрал этот пункт из маршрута, остальное по твоему порядку")
        if dropped:
            best = refine(seq, A, B, now, deadline, ctx)
    if not best:
        fin = relaxed["finish"]
        why = (f"к {deadline:%H:%M} не успеть: по этим пунктам будешь в конце только в {_fmt(fin)}" if has_deadline and fin > deadline
               else "кто-то из пунктов будет закрыт к твоему приходу")
        return {"error": f"Так по порядку не выйдет — {why}. Сократи время в пунктах или поменяй порядок.", "notes": []}
    for s in best["steps"]:
        if s["type"] == "stop" and s["source"] == "твой пункт маршрута":
            s["hours"] = {"status": "open", "confidence": "outdoor", "source": "—", "text": "твой пункт — часы не проверяю"}
    notes = ["Маршрут по твоим пунктам в твоём порядке — ничего не подбирал. Время в каждом месте можно указать: «в Меге час»."]
    for d in dropped:
        notes.insert(0, d + ".")
    stops = [s for s in best["steps"] if s["type"] == "stop"]
    for v in intent["via"]:
        if v.get("by"):
            s = next((s for s in stops if s["name"] == v["label"]), None)
            if s and s["arrive"] > v["by"]:
                notes.insert(0, f"К {v['by']} в «{v['label']}» не успеть: будешь там в {s['arrive']}. Выйди пораньше.")
            elif s:
                notes.insert(0, f"В «{v['label']}» будешь в {s['arrive']} — к {v['by']} успеваешь.")
    if best.get("_tight"):
        notes.insert(0, f"Впритык: будешь на месте в {_fmt(best['finish'])}, без запаса. Лучше сократить время в одном из пунктов.")
    if not has_deadline and not any(v.get("by") for v in intent["via"]):
        notes.append(f"Срока ты не назвал — просто посчитал: в конце будешь в {_fmt(best['finish'])}.")
    return {"steps": best["steps"], "notes": notes,
            "summary": {"arrive": _fmt(best["finish"]), "buffer_min": best["slack"] if has_deadline else 0,
                        "outdoor_min": best["outdoor"], "walk_km": best["walk_km"], "max_outdoor_leg": best["max_outdoor_leg"],
                        "taxis": best["taxis"], "window_min": round((best["finish"] - now).total_seconds() / 60)},
            "cold": ctx.cold, "max_leg": ctx.max_leg, "chain": True,
            "considered": {"candidates": len(seq), "plans": 1, "pois_total": len(POIS), "rejected": {}, "rejected_ex": {}}}


MEET_DEFAULT = {"кафе", "ресторан", "торговый центр"}


def _travel_min(a, b, t: datetime, ctx: Ctx) -> tuple[float, str]:
    walk = geo.walk_min_est(a, b) * ctx.pace
    if walk <= ctx.max_leg:
        return walk, "walk"
    return geo.car_min_est(a, b, t) + geo.TAXI_WAIT_MIN, "taxi"


def meet(intent: dict, now: datetime, w: dict, friend: dict) -> dict:
    """«Встретиться посередине»: место, куда обоим добираться примерно одинаково и которое открыто, когда вы придёте.
    Цена места = самый долгий путь из двух + половина разницы − «качество» места (тот же счёт, что у обычных остановок)."""
    ctx = Ctx(intent, now, w)
    A = (intent["origin"]["lat"], intent["origin"]["lon"])
    F = (friend["lat"], friend["lon"])
    cats = (ctx.wanted - {"достопримечательность", "памятник", "парк", "сад", "фонтан", "смотровая", "детская площадка"}) \
        if ctx.prefs else set(MEET_DEFAULT)
    cats = cats or set(MEET_DEFAULT)
    mid = ((A[0] + F[0]) / 2, (A[1] + F[1]) / 2)
    span = geo.haversine_km(A, F)
    stay = 60
    cand = []
    for p in POIS:
        if p["category"] not in cats or p.get("unnamed"):
            continue
        pt = (p["lat"], p["lon"])
        if geo.haversine_km(mid, pt) > max(1.2, span * 0.45):
            continue
        sc = stop_score(p, ctx)
        if sc is None:
            continue
        ta, ma = _travel_min(A, pt, now, ctx)
        tf, mf = _travel_min(F, pt, now, ctx)
        meet_t = now + timedelta(minutes=max(ta, tf))
        st, conf, _ = interval_status(p.get("tags"), p["category"], meet_t, meet_t + timedelta(minutes=stay))
        if st != "open":
            continue
        cand.append((max(ta, tf) + 0.5 * abs(ta - tf) - sc * 3, p, ta, ma, tf, mf))
    if not cand:
        return {"error": "Открытого места посередине, куда вам обоим удобно добраться, в данных не нашёл. "
                         "Попробуй другое время или напиши, где хотите встретиться."}
    cand.sort(key=lambda x: x[0])
    _, p, ta, ma, tf, mf = cand[0]
    meet_t = now + timedelta(minutes=max(ta, tf))
    start = meet_t - timedelta(minutes=ta)    # кому ближе — тот выходит позже, чтобы прийти одновременно
    ctx.bonus = {p["id"]: stay}
    best = refine([p], A, (p["lat"], p["lon"]), start, start + timedelta(hours=8), ctx)
    if not best:
        return {"error": "Не получилось посчитать дорогу до места встречи — попробуй ещё раз."}
    fr = geo.route(F, (p["lat"], p["lon"]), "foot" if mf == "walk" else "car")   # путь друга — для карты
    coords = fr["coords"] if fr else [[F[1], F[0]], [p["lon"], p["lat"]]]
    f_start = meet_t - timedelta(minutes=tf)
    how = lambda m, mode: f"~{round(m)} мин {'пешком' if mode == 'walk' else 'на такси'}"  # noqa: E731
    alt = [c[1]["name"] for c in cand[1:4] if c[1]["name"] != p["name"]][:2]
    notes = [f"Встреча в «{p['name']}» в {_fmt(meet_t)}: тебе {how(ta, ma)} — выходи в {_fmt(start)}; "
             f"из «{friend['label']}» {how(tf, mf)} — выходить в {_fmt(f_start)}. Место выбрано так, чтобы дорога у обоих была примерно равной.",
             *([f"Ещё варианты посередине: {', '.join(alt)}."] if alt else [])]
    if best["taxis"] or mf == "taxi":
        f_, label = geo.traffic_factor(now)
        notes.append(f"Время на такси — оценка с поправкой на пробки ({label}, ×{f_}) и подачу машины ~{geo.TAXI_WAIT_MIN} мин.")
    return {
        "steps": best["steps"], "notes": notes,
        "summary": {"arrive": _fmt(best["finish"]), "buffer_min": 0, "outdoor_min": best["outdoor"],
                    "walk_km": best["walk_km"], "max_outdoor_leg": best["max_outdoor_leg"], "taxis": best["taxis"],
                    "window_min": round((best["finish"] - start).total_seconds() / 60)},
        "cold": ctx.cold, "max_leg": ctx.max_leg, "start_at": _fmt(start),
        "considered": {"candidates": len(cand), "plans": len(cand), "pois_total": len(POIS), "rejected": ctx.rejected,
                       "rejected_ex": {}},
        "friend": {"label": friend["label"], "lat": F[0], "lon": F[1], "mode": mf, "min": round(tf), "leave": _fmt(f_start),
                   "coords": coords},
        "meet": {"name": p["name"], "time": _fmt(meet_t), "you_min": round(ta), "friend_min": round(tf)},
    }


def plan_after_first(intent: dict, now: datetime, w: dict, first: dict) -> dict:
    """«Сначала в музей, потом что-то ещё»: человек начинает с названного места (побыть там), остальное подбираем после него."""
    ctx = Ctx(intent, now, w)
    stay = int(first.get("_stay") or STAY.get(first["category"], 45))
    ctx.bonus = {first["id"]: stay}
    A = (first["lat"], first["lon"])
    head = refine([first], A, A, now, now + timedelta(minutes=stay + 30), ctx)
    if not head:
        # место закрыто (музей в понедельник) — честно говорим и подбираем другое
        st, conf, text = interval_status(first.get("tags"), first["category"], now, now + timedelta(minutes=stay))
        rest = plan(intent, now, w)
        if not rest.get("error"):
            why = text if st == "closed" else "к твоему приходу не успеть до закрытия"
            rest["notes"] = [f"{first['name']}: {why} — поэтому подобрал другие места на сегодня."] + (rest.get("notes") or [])
        return rest
    stop = next(s for s in head["steps"] if s["type"] == "stop")
    rest = plan(intent, now + timedelta(minutes=stay), w)
    if rest.get("error"):
        return rest
    rest["steps"] = [rest["steps"][0], {**stop, "facts": {**stop.get("facts", {}), "твой_пункт": "человек сам попросил начать отсюда"}}] + rest["steps"][1:]
    rest["notes"] = [f"Начинаем с места, которое ты назвал: {first['name']}, ~{stay} мин; дальше — подобрал сам."] + (rest.get("notes") or [])
    rest["summary"]["window_min"] = rest["summary"].get("window_min", 0) + stay
    return rest


def plan(intent: dict, now: datetime, w: dict, exclude=(), required=()):
    ctx = Ctx(intent, now, w)
    ctx.window = (datetime.fromisoformat(intent["deadline"]) - now).total_seconds() / 60
    A = (intent["origin"]["lat"], intent["origin"]["lon"])
    B = (intent["destination"]["lat"], intent["destination"]["lon"])
    start = now
    deadline = datetime.fromisoformat(intent["deadline"])
    window = (deadline - start).total_seconds() / 60
    notes = []
    direct_walk = geo.route(A, B, "foot") if geo.haversine_km(A, B) > 0.3 else None
    if window < 10:
        return {"error": "Окно слишком короткое: меньше 10 минут. " + (
            "На остановку не хватит — можно просто выйти подышать." if geo.haversine_km(A, B) < 0.3 else "Лучше просто идти к цели.")}
    if geo.haversine_km(A, B) < 0.3 and window < 120 and not ctx.explicit and \
            not (ctx.prefs & {"active", "cinema", "kids", "night", "culture", "food"}):
        ctx.walk_only = intent.get("mobility") != "limited" and not intent.get("_taxi_ok")  # прогулка по кругу — такси туда-обратно бессмысленно (кроме маломобильных)
    max_stops = intent.get("max_stops") or (3 if window >= 100 else 2 if window >= 50 else 1)
    focused = ctx.prefs & {"food", "coffee", "work", "night", "kids", "shopping"} and \
        not ctx.prefs & {"sights", "walk", "view", "unusual", "culture", "nature", "romantic"} and not ctx.explicit
    if focused:
        both = {"food", "coffee"} <= ctx.prefs or "kids" in ctx.prefs
        max_stops = min(max_stops, 2 if both and window >= 120 else 1)   # «поесть перед концертом» — одно место, без памятника
    if intent.get("mobility") == "limited":
        max_stops = 1   # «не спеша», «нет сил», коляска — одно хорошее место и подольше, без беготни между точками
    if ctx.work:
        max_stops = 1
        # поработать: сидеть как можно дольше — пробуем 80% окна, если рядом нет такого места — меньше
        WORK = {"кафе", "коворкинг", "библиотека"}
        for k in (0.8, 0.65, 0.5, 0.35):
            ctx.work_stay = int(max(30, min(240, window * k)) // 10 * 10)
            results, n_cands = search(A, B, start, deadline, ctx, exclude, required, max_stops)
            results = [r for r in results if any(p["category"] in WORK for p in r[1])]   # парк для ноутбука — не ответ
            if results:
                break
    else:
        results, n_cands = search(A, B, start, deadline, ctx, exclude, required, max_stops)
        if focused:   # просили конкретное (бар вечером, поесть) — план без такого места не годится, если есть план с ним
            want = set().union(*(PREF_CATS.get(x, set()) for x in ctx.prefs & {"food", "coffee", "night", "kids", "shopping"}))
            want -= {"достопримечательность", "смотровая", "фонтан", "парк"}
            hit = [r for r in results if any(p["category"] in want for p in r[1])]
            if not hit and ctx.prefs & {"food", "coffee"}:
                ctx.quick_food = True   # «поесть за 40 минут»: посидеть не успеть, но быстро перекусить — успеешь
                r2, n2 = search(A, B, start, deadline, ctx, exclude, required, max_stops)
                hit = [r for r in r2 if any(p["category"] in want for p in r[1])]
                if hit:
                    notes.append("Времени впритык — успеешь быстро перекусить (~12 мин) или взять с собой.")
                else:
                    ctx.quick_food = False
            if hit:
                results = hit
    if ctx.explicit & SHORT_OK and not any(any(p["category"] in ctx.explicit for p in r[1]) for r in results[:5]):
        ctx.short_explicit = True  # названное место не помещается — пробуем короткий заход (10 мин), а не подмену
        r2, n2 = search(A, B, start, deadline, ctx, exclude, required, max_stops)
        if any(any(p["category"] in ctx.explicit for p in r[1]) for r in r2[:5]):
            results, n_cands = r2, n2
        else:
            ctx.short_explicit = False
    distinct, seen = [], {}
    for item in results:  # убираем дубли, но для каждого набора мест держим 2 порядка: один может не пройти точный расчёт
        key = frozenset((p["name"], round(p["lat"], 3)) for p in item[1])
        if seen.get(key, 0) < 2:
            seen[key] = seen.get(key, 0) + 1
            distinct.append(item)
        if len(seen) >= 8 or len(distinct) >= 14:
            break
    _enrich(distinct, ctx)
    _prefetch(distinct[:4], A, B)
    best = None
    for score, seq, _ in distinct:
        best = refine(seq, A, B, start, deadline, ctx)
        if best:
            break
    # большой запас при одной остановке, где можно посидеть (кафе, парк, музей…) — отдаём его месту: «45 минут на кофе»
    # значит посидеть ~35 минут, а не 20 и вернуться за 20 минут до срока
    EXTEND = {"кафе", "ресторан", "бар", "парк", "сад", "музей", "галерея", "библиотека", "торговый центр", "детский центр", "книжный"}
    if best and not ctx.work:
        stops_b = [x for x in best["steps"] if x["type"] == "stop"]
        if len(stops_b) == 1 and stops_b[0]["category"] in EXTEND and best["slack"] > 18:
            sid = stops_b[0]["id"]
            new = stops_b[0]["stay"] + min(best["slack"] - 12, 60)
            ctx.bonus = {sid: int(new // 5 * 5)}
            longer = refine([q for q in POIS if q["id"] == sid], A, B, start, deadline, ctx)
            if longer and longer["slack"] >= 8:
                best = longer
            else:
                ctx.bonus = {}
    geo.save_cache()
    if direct_walk:
        dw = round(direct_walk["min"])
        if dw > window:
            notes.append(f"Пешком напрямую {dw} мин, а окно {round(window)} мин: без такси не успеть.")
        elif dw > window * 0.6:
            notes.append(f"Пешком напрямую {dw} мин из {round(window)}: на остановки времени мало, часть пути — на такси.")
    if not best and not intent.get("_relaxed"):
        # сами делаем то, что раньше советовали человеку («убери пожелания»): сначала разрешаем такси на прогулке,
        # потом «кофе и розетка» → просто посидеть с кофе. И честно пишем, что ослабили
        soft = None
        if ctx.walk_only:
            soft = ({**intent, "_relaxed": True, "transport": "any", "_taxi_ok": True},
                    "Пешком туда и обратно за это окно ничего интересного рядом нет — подобрал место, куда быстрее доехать на такси.")
        elif ctx.prefs & {"work", "quiet"}:
            soft = ({**intent, "_relaxed": True, "prefs": sorted((set(intent.get("prefs") or []) - {"work", "quiet"}) | {"coffee"})},
                    "Тихого места с розеткой рядом в данных нет — вот ближайшее, где можно посидеть с кофе; розетку уточни на месте.")
        if soft:
            r2 = plan(soft[0], now, w, exclude, required)
            if not r2.get("error"):
                r2["notes"].insert(0, soft[1])
                return r2
    if not best:
        return {"error": _no_plan_advice(A, B, start, window, direct_walk, ctx), "notes": notes, "tip": getattr(ctx, "tip", None)}
    if ctx.pace > 1.2:
        notes.append("Время пешком посчитано для спокойного темпа (≈3,3 км/ч) и коротких отрезков — с коляской или когда трудно ходить.")
    elif ctx.pace > 1:
        notes.append("С ребёнком шаг медленнее — время пешком посчитано с запасом (≈3,9 км/ч).")
    if (w.get("aqi") or 0) > 150:
        notes.append(f"Воздух сейчас плохой (индекс AQI {w['aqi']}, Open-Meteo): меньше улицы, больше крытых мест.")
    if ctx.cold != "ok" and ((w.get("apparent") or 0) <= -7 or (w.get("wind") or 0) >= 30 or (w.get("precip") or 0) > 0.5):
        a = w.get("apparent")
        notes.append(f"Ощущается как {round(a):+d}°{', ветер ' + str(round(w['wind'])) + ' км/ч' if w.get('wind') else ''}: "
                     f"на улице не больше {ctx.max_leg} мин подряд, уличные места — только если без них никак.")
    jam = next((s["traffic"] for s in best["steps"] if s["type"] == "leg" and s["mode"] == "taxi"
                and s["traffic"].startswith("пробки 2GIS")), None)
    if jam:
        notes.append(f"Время на такси — по данным о пробках 2GIS ({jam.replace('пробки 2GIS', '').strip(' :') or 'сейчас'}) "
                     f"плюс подача машины ~{geo.TAXI_WAIT_MIN} мин.")
    elif best["taxis"]:
        f, label = geo.traffic_factor(now)
        notes.append(f"Время на такси — оценка с поправкой на пробки ({label}, ×{f}) и подачу машины ~{geo.TAXI_WAIT_MIN} мин.")
    if ctx.is_dark(now + timedelta(minutes=window / 2)):
        notes.append("Будет темно: площадки и малоизвестные парки не предлагаю — только освещённые центральные и крытые места.")
    stops = [s for s in best["steps"] if s["type"] == "stop"]
    got = {s["category"] for s in stops}
    names = {s["name"] for s in stops}
    if ctx.i.get("kazakh_food") and not any("казах" in " ".join(s.get("facts", {}).get("отметки_2гис") or []).lower()
                                            for s in stops):
        notes.insert(0, "Мест с отметкой «казахская кухня» по пути в данных нет — показываю, где поесть рядом; "
                        "национальные блюда уточни в меню.")
    if "view" in ctx.prefs and not (names & VIEW_POINTS):
        notes.insert(0, "Лучший вид на город — смотровая Байтерека (97 м), но в это окно до неё не успеть туда и обратно. "
                        "Предлагаю, что есть рядом.")
    if "unusual" in ctx.prefs and not (got & {"квест", "океанариум", "арт-центр", "развлечения"}):
        notes.insert(0, "Необычного места (квест, океанариум, арт-пространство), открытого к твоему приходу, рядом не нашёл — "
                        "предлагаю лучшее из того, что успеешь.")
    hit_any = any(got & PREF_CATS.get(pr, set()) for pr in ctx.prefs if pr in UNMET and pr not in ("view", "unusual"))
    for cat in ctx.explicit - got:
        near = min((p for p in POIS if p["category"] == cat),
                   key=lambda p: geo.haversine_km(A, (p["lat"], p["lon"])), default=None)
        where = f" Ближайшее — {near['name']}, {geo.haversine_km(A, (near['lat'], near['lon'])):.1f} км: " \
                "туда и обратно с остановкой в это окно не помещается." if near else ""
        # честная причина: если такие места в это окно закрыты (музей в 6 утра) — так и говорим, а не «не хватает времени»
        if near:
            stay = timedelta(minutes=STAY.get(cat, 30))
            st1, conf, txt = interval_status(near.get("tags"), cat, start, start + stay)
            st2, _, _ = interval_status(near.get("tags"), cat, max(start, deadline - stay), deadline)
            if st1 != "open" and st2 != "open":
                hrs = txt.split(" · ")[0].strip()
                hrs = f" ({near['name']}: {hrs})" if hrs else f" ({near['name']})"
                closed = "в это время такие места закрыты" if conf == "verified" else "в это время такие места обычно закрыты (типичные часы, точных данных нет)"
                notes.insert(0, f"Просил «{cat}», но {closed}{hrs}. Предлагаю, что работает сейчас; туда лучше прийти в другое время.")
                hit_any = True
                continue
        notes.insert(0, f"Просил «{cat}», но по времени не получается.{where} Предлагаю, что успеешь рядом.")
        hit_any = True
    for pref, word in UNMET.items():
        if pref in ctx.prefs and not (got & PREF_CATS[pref]) and not hit_any:
            near = _nearest_of(PREF_CATS[pref], A, start, ctx)
            if near:   # конкретно: что ближайшее и почему не поместилось — полезнее, чем «не нашёл»
                p0, walk, need = near
                notes.insert(0, f"{word} в это окно не помещается: ближайшее — {p0['name']}, {round(walk)} мин пешком; "
                                f"туда, {stay_of(p0, ctx)} мин там и обратно — около {round(need)} мин, а окно {round(window)}. "
                                "Предлагаю, что успеешь рядом.")
            else:
                notes.insert(0, f"{word} по пути, открытых к твоему приходу, не нашёл — предлагаю то, что работает. Это честно, а не сбой.")
            break
    est = sum(1 for s in stops if s["hours"]["confidence"] == "estimate")
    if est:
        notes.append(f"У {est} из {len(stops)} мест часов работы нет в данных — показал типичные, помечено «оценка».")
    return {
        "steps": best["steps"], "notes": notes,
        "summary": {"arrive": _fmt(best["finish"]), "buffer_min": best["slack"], "outdoor_min": best["outdoor"],
                    "walk_km": best["walk_km"], "max_outdoor_leg": best["max_outdoor_leg"], "taxis": best["taxis"],
                    "window_min": round(window)},
        "cold": ctx.cold, "max_leg": ctx.max_leg,
        "considered": {"candidates": n_cands, "plans": len(results), "pois_total": len(POIS), "rejected": ctx.rejected,
                       "rejected_ex": {k: [n for _, n in v] for k, v in ctx.rejected_ex.items()}},
    }
