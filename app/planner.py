"""Планировщик «Окна»: из точки А в точку Б к сроку, с 1–3 остановками, которые точно успеваешь.

Это «R» в RAG: жёсткие проверки делает код (время, часы, погода, расстояния), а ИИ потом только
объясняет выбор человеческим языком — и только по фактам, которые вернул планировщик.
"""
from __future__ import annotations

import itertools
from concurrent.futures import ThreadPoolExecutor
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import dgis, geo
from .hours import OUTDOOR, interval_status

ROOT = Path(__file__).resolve().parent.parent
POIS: list[dict] = json.loads((ROOT / "data" / "poi.json").read_text(encoding="utf-8"))


def _merge_2gis() -> int:
    """Популярные места из 2GIS (scripts/collect_2gis.py, локальный файл): если место уже есть в OSM — даём ему
    рейтинг и свежие часы; если нет — добавляем как новое с источником 2GIS. Без файла система работает на одном OSM."""
    f = ROOT / "data" / "dgis_pois.json"
    if not f.exists():
        return 0
    grid: dict[tuple, list] = {}
    for p in POIS:
        grid.setdefault((round(p["lat"], 2), round(p["lon"], 2)), []).append(p)
    added = 0
    for it in json.loads(f.read_text(encoding="utf-8")).values():
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
        best["wiki"] = {"sitelinks": w["sitelinks"], "extract": w.get("extract"), "article": w.get("article")}
        best["tags"].setdefault("wikidata", w["qid"])
    return added


WIKI_ADDED = _merge_wiki()


def _inside_malls():
    """Аттракционы, отмеченные в OSM точками внутри ТРЦ (горки в Хан Шатыре и т.п.), — это не уличные
    достопримечательности: они работают по часам ТРЦ. Переносим часы ТРЦ и помечаем «внутри»."""
    malls = [p for p in POIS if p["category"] == "торговый центр"]
    for p in POIS:
        if p["category"] not in ("достопримечательность", "развлечения", "парк развлечений", "кафе", "ресторан",
                                 "фастфуд", "кино"):
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
    "парк развлечений": 45, "батутный парк": 45, "аквапарк": 90,
}
PREF_CATS = {
    "coffee": {"кафе"},
    "food": {"ресторан", "кафе", "фастфуд"},
    "sights": {"достопримечательность", "памятник", "смотровая", "фонтан", "мечеть", "собор"},
    "nature": {"парк", "сад"},
    "culture": {"музей", "галерея", "арт-центр", "библиотека", "театр"},
    "shopping": {"торговый центр", "книжный"},
    "kids": {"детская площадка", "парк развлечений", "развлечения", "батутный парк", "океанариум",
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
}
UNMET = {"coffee": "Кафе", "food": "Места поесть", "work": "Места для работы", "culture": "Музея или галереи",
         "cinema": "Кино", "kids": "Детского места", "night": "Бара или ночного места"}
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
        # темп: коляска / трудно ходить — медленнее и короче отрезки; с ребёнком — тоже медленнее
        self.pace = 1.0
        if intent.get("mobility") == "limited":
            self.pace, self.max_leg = 1.35, min(self.max_leg, 15)
        elif intent.get("with_kids"):
            self.pace = 1.15
        self.prefs = set(intent.get("prefs") or [])
        if intent.get("with_kids"):
            self.prefs.add("kids")
        far = geo.haversine_km((intent["origin"]["lat"], intent["origin"]["lon"]),
                               (intent["destination"]["lat"], intent["destination"]["lon"])) > 3
        # «погулять» по пути в аэропорт не значит «идти до аэропорта пешком»
        self.walk_only = intent.get("transport") == "walk" or ("walk" in self.prefs and not far)
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


VIEW_POINTS = {"Байтерек"}  # смотровая площадка на высоте 97 м (Википедия); в OSM размечен как памятник
GENERIC = {"сквер", "парк", "фонтан", "памятник", "скульптура", "монумент", "кафе", "столовая", "бар", "ресторан",
           "кофейня", "кафетерий", "буфет", "сад", "аллея"}


def stop_score(p: dict, ctx: Ctx, arrive: datetime | None = None) -> float | None:
    cat = p["category"]
    tags = p.get("tags", {})
    if cat not in ctx.wanted:
        return None
    if p["name"].strip().lower() in GENERIC:
        return None  # «Сквер» без названия: человек не найдёт и не поймёт, куда идти
    s = 1.0
    if ctx.prefs and any(cat in PREF_CATS.get(pr, ()) for pr in ctx.prefs):
        s += 1.5
    if cat in ctx.explicit:
        s += 3.0  # человек прямо назвал тип места
    if "view" in ctx.prefs and p["name"] in VIEW_POINTS:
        s += 2.5  # смотровая площадка — именно «вид на город»
    if "quiet" in ctx.prefs and cat in ("торговый центр", "бар", "ночной клуб", "фастфуд"):
        return None  # просили тихо — не ведём в шумные места
    if "unusual" in ctx.prefs and cat in ("квест", "океанариум", "арт-центр", "развлечения"):
        s += 1.0
    s += min(p.get("richness", 0), 10) * 0.08
    sightseeing = not ctx.prefs or bool(ctx.prefs & {"sights", "culture", "walk", "nature"})
    if "wikidata" in tags:
        s += 0.8 if sightseeing else 0.2
    if p.get("wiki") and sightseeing:
        s += min(p["wiki"]["sitelinks"], 30) * 0.03  # статьи на 30 языках — +0,9: символ города важнее безымянного сквера
    if "kids" in ctx.prefs and cat in ("детская площадка", "развлечения", "батутный парк", "океанариум", "зоопарк",
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
    outdoor = cat in OUTDOOR
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
            if "kids" in ctx.prefs or cat == "детская площадка":
                return None
            s -= 0.8
    if tags.get("brand"):
        s -= 0.15  # чуть выше местные места, чем сети
    return s


def stay_of(p: dict, ctx: "Ctx") -> int:
    if ctx.work and p["category"] in ("кафе", "коворкинг", "библиотека"):
        return ctx.work_stay
    if getattr(ctx, "short_explicit", False) and p["category"] in ctx.explicit:
        return 10
    if p["category"] in ("парк", "сад") and p["category"] in getattr(ctx, "explicit", ()):
        w = getattr(ctx, "window", 60) or 60
        return max(15, min(45, int(w * 0.3) // 5 * 5))  # «погулять в парке» — парк и есть цель прогулки
    if "walk" in ctx.prefs and p["category"] in ("парк", "сад", "фонтан", "памятник", "достопримечательность", "смотровая"):
        return 10  # прогулка: место — точка маршрута, а не цель
    st = STAY.get(p["category"], 20)
    w = getattr(ctx, "window", None)
    if w and w <= 45 and p["category"] not in LONG_STAYS:
        st = max(10, min(st, int(w * 0.5) // 5 * 5))  # короткое окно — короткая остановка, а не отказ
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
    if meals > 1 or cafes > 1 or (meals and cafes and not ctx.work and len(seq) > 2):
        return None  # три еды подряд — не прогулка; максимум одна «поесть» и одна «кофе»
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
    buffer = max(7.0, window * 0.09)  # запас на то, что реальный путь по улицам длиннее оценки
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
    if ctx.explicit and not any(p["category"] in ctx.explicit for p in seq):
        score -= 5.0  # «хочу в музей» — план без музея только если музей никак не помещается
    return {"score": score, "finish": t, "legs": legs, "stops": stops, "slack": slack}


def _nm(s: str) -> str:
    return re.sub(r"[^a-zа-яёәіңғүұқөһ0-9]", "", (s or "").lower().split("(")[0])


def _same_place(p: dict, label: str) -> bool:
    """«Хан Шатыр» как остановка, когда человек и так в Хан Шатыре, — бессмысленно."""
    a, b = _nm(p["name"]), _nm(label)
    return len(a) >= 4 and len(b) >= 4 and (a in b or b in a)


def _candidates(A, B, start, deadline, ctx: Ctx, exclude: set, required: set):
    window = (deadline - start).total_seconds() / 60
    loop = geo.haversine_km(A, B) < 0.3
    o_label = (ctx.i.get("origin") or {}).get("label", "")
    d_label = (ctx.i.get("destination") or {}).get("label", "")
    out = []
    for p in POIS:
        if p["id"] in exclude:
            continue
        pt0 = (p["lat"], p["lon"])
        if p["id"] not in required and (_same_place(p, o_label) or (not loop and _same_place(p, d_label))):
            continue
        if loop and p["id"] not in required:
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
            continue
        sc = stop_score(p, ctx) or 0
        detour = geo.haversine_km(A, pt) + geo.haversine_km(pt, B) - geo.haversine_km(A, B)
        out.append((sc - detour * 0.35, p))
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


def search(A, B, start, deadline, ctx: Ctx, exclude=frozenset(), required=frozenset(), max_stops=3):
    cands = _candidates(A, B, start, deadline, ctx, set(exclude), set(required))
    loop = geo.haversine_km(A, B) < 0.3
    results = []
    for k in range(1, max_stops + 1):
        for combo in itertools.combinations(cands, k):
            if required and not set(required) <= {p["id"] for p in combo}:
                continue
            if loop:
                orders = itertools.permutations(combo) if k > 1 else [combo]
            else:
                orders = [sorted(combo, key=lambda p: _projection(A, B, p))]
            for seq in orders:
                sim = simulate(list(seq), A, B, start, deadline, ctx)
                if sim:
                    results.append((sim["score"], list(seq), sim))
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
            if r and r["min"] > ctx.max_leg and not ctx.walk_only and r["min"] > geo.walk_min_est(pos, pt) * ctx.pace * 1.25:
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
        steps.append(leg)
        t += timedelta(minutes=leg["min"])
        if i < len(seq):
            p = seq[i]
            stay = stay_of(p, ctx)
            arrive, leave = t, t + timedelta(minutes=stay)
            st, conf, text = interval_status(p.get("tags"), p["category"], arrive, leave)
            if st in ("closed", "closes_soon"):
                return None
            steps.append({"type": "stop", "id": p["id"], "name": p["name"], "category": p["category"],
                          "lat": p["lat"], "lon": p["lon"], "arrive": _fmt(arrive), "leave": _fmt(leave), "stay": stay,
                          "hours": {"status": st, "confidence": conf, "source": p.get("hours_src", "OSM"),
                                    "text": text + (" · по 2GIS" if p.get("hours_src") == "2GIS" else "")},
                          "rating": (p.get("dgis") or {}).get("rating"), "reviews": (p.get("dgis") or {}).get("reviews"),
                          "dgis_id": (p.get("dgis") or {}).get("id"),
                          "wiki_url": (p.get("wiki") or {}).get("article"),
                          "facts": _facts(p) | ({"по_пути": f"крюк всего {d} мин" if d >= 2 else "прямо по пути, без крюка"} if (d := _detour_min(A, B, p)) <= 8
                                                and geo.haversine_km(A, B) >= 0.3 else {}) | {"остановка": f"{i + 1} из {len(seq)}",
                                                "в_помещении": p["category"] not in OUTDOOR},
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
            out["известность"] = f"статьи о месте есть в Википедии на {w['sitelinks']} языках"
        if w.get("extract"):
            out["википедия"] = w["extract"]
    elif "wikidata" in t or "wikipedia" in t:
        out["известность"] = "есть статья в Википедии — известное место города"
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


def _no_plan_advice(A, B, start, window, direct_walk, ctx) -> str:
    """Честный и полезный отказ: почему не вышло и что делать вместо этого."""
    if geo.haversine_km(A, B) < 0.3:
        if window < 25:
            return f"Окно {round(window)} мин — на остановку с возвращением не хватает. Можно просто выйти подышать на 10 минут."
        return ("Рядом нет подходящих мест, до которых можно дойти и вернуться за это время"
                + (" с учётом твоих пожеланий" if ctx.prefs else "") + ". Попробуй убрать пожелания или увеличить окно.")
    walk = round(direct_walk["min"]) if direct_walk else round(geo.walk_min_est(A, B))
    taxi = round(geo.car_min_est(A, B, start) + geo.TAXI_WAIT_MIN)
    if walk <= window:
        return f"На остановки времени нет: пешком до цели {walk} мин из {round(window)}. Просто иди — успеешь с запасом {round(window) - walk} мин."
    if taxi <= window:
        return f"Пешком не успеть ({walk} мин при окне {round(window)}). На такси ~{taxi} мин с подачей и пробками (оценка) — лучше ехать сразу."
    return f"Не успеть даже на такси: ~{taxi} мин (оценка), а окно {round(window)} мин. Стоит предупредить, что опоздаешь."


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
        return {"error": "Окно слишком короткое: меньше 10 минут. Лучше просто идти к цели."}
    if geo.haversine_km(A, B) < 0.3 and window < 120:
        ctx.walk_only = True  # прогулка по кругу — такси туда-обратно бессмысленно
    max_stops = intent.get("max_stops") or (3 if window >= 100 else 2 if window >= 50 else 1)
    if ctx.work:
        max_stops = 1
        ctx.work_stay = int(max(30, min(150, window * 0.6)) // 10 * 10)
    results, n_cands = search(A, B, start, deadline, ctx, exclude, required, max_stops)
    if ctx.explicit and not any(any(p["category"] in ctx.explicit for p in r[1]) for r in results[:5]):
        ctx.short_explicit = True  # названное место не помещается — пробуем короткий заход (10 мин), а не подмену
        r2, n2 = search(A, B, start, deadline, ctx, exclude, required, max_stops)
        if any(any(p["category"] in ctx.explicit for p in r[1]) for r in r2[:5]):
            results, n_cands = r2, n2
        else:
            ctx.short_explicit = False
    distinct, seen = [], set()
    for item in results:  # убираем дубли: тот же набор мест в другом порядке или одноимённые точки
        key = frozenset((p["name"], round(p["lat"], 3)) for p in item[1])
        if key not in seen:
            seen.add(key)
            distinct.append(item)
        if len(distinct) >= 8:
            break
    _enrich(distinct, ctx)
    _prefetch(distinct[:4], A, B)
    best = None
    for score, seq, _ in distinct:
        best = refine(seq, A, B, start, deadline, ctx)
        if best:
            break
    geo.save_cache()
    if direct_walk:
        dw = round(direct_walk["min"])
        if dw > window:
            notes.append(f"Пешком напрямую {dw} мин, а окно {round(window)} мин: без такси не успеть.")
        elif dw > window * 0.6:
            notes.append(f"Пешком напрямую {dw} мин из {round(window)}: на остановки времени мало, часть пути — на такси.")
    if not best:
        return {"error": _no_plan_advice(A, B, start, window, direct_walk, ctx), "notes": notes}
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
        notes.append("Будет темно: парки и площадки не предлагаю, только освещённые и крытые места.")
    stops = [s for s in best["steps"] if s["type"] == "stop"]
    got = {s["category"] for s in stops}
    names = {s["name"] for s in stops}
    if "view" in ctx.prefs and not (names & VIEW_POINTS):
        notes.insert(0, "Лучший вид на город — смотровая Байтерека (97 м), но в это окно до неё не успеть туда и обратно. "
                        "Предлагаю, что есть рядом.")
    if "unusual" in ctx.prefs and not (got & {"квест", "океанариум", "арт-центр", "развлечения"}):
        notes.insert(0, "Необычного места (квест, океанариум, арт-пространство), открытого к твоему приходу, рядом не нашёл — "
                        "предлагаю лучшее из того, что успеешь.")
    hit_any = any(got & PREF_CATS.get(pr, set()) for pr in ctx.prefs if pr in UNMET and pr not in ("view", "unusual"))
    for cat in ctx.explicit - got:
        notes.insert(0, f"Просил «{cat}» — подходящего по времени и часам работы не нашёл, предлагаю ближайшее похожее.")
        hit_any = True
    for pref, word in UNMET.items():
        if pref in ctx.prefs and not (got & PREF_CATS[pref]) and not hit_any:
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
        "considered": {"candidates": n_cands, "plans": len(results), "pois_total": len(POIS)},
    }
