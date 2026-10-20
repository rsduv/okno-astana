"""Сервер «Окна». Запуск: python -m uvicorn app.main:app --port 8000  →  http://localhost:8000"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import citystats, geo, llm, lrt, planner, events

ROOT = Path(__file__).resolve().parent.parent
KZ = timezone(timedelta(hours=5))  # Казахстан живёт по UTC+5
app = FastAPI(title="Окно — свободное время в Астане")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class PlanReq(BaseModel):
    text: str = ""
    point: dict | None = None        # {"lat","lon"} — если человек ткнул в карту
    now: str | None = None           # "17:20" — подменить текущее время (для демо)
    weather: str = "now"             # "now" | "cold" (симуляция −25°)
    exclude: list[str] = []          # «заменить» остановку
    keep: list[str] = []             # оставить остальные остановки
    parsed: dict | None = None       # уже разобранный запрос (чтобы не звать ИИ повторно)
    saved: dict = {}                 # свои места пользователя {"Дом": {"lat","lon"}} — живут только в его браузере


# тип места, прямо названный в запросе, — главный приоритет («хочу в музей» → сначала музеи, а не просто известные места)
EXPLICIT = [(r"музе|museum|мұражай", "музей"), (r"театр|theat|спектакл", "театр"), (r"кино|cinema|movie|фильм", "кино"),
            (r"галере|gallery|выставк", "галерея"), (r"мечет|mosque|мешіт", "мечеть"), (r"собор|церк|church|cathedral", "собор"),
            (r"библиотек|library", "библиотека"), (r"\bбар(?:а|е|ы|ов|ам)?\b|\bbars?\b|\bпаб|\bpub|\bпив[оау]|\bbeer|коктейл|cocktail|вин[оа]\b|\bwine", "бар"), (r"боулинг|bowling", "боулинг"),
            (r"каток|skating", "каток"), (r"коворкинг|coworking", "коворкинг"), (r"смотров|viewpoint|observation", "смотровая"), (r"\bпарк|\bpark\b|сквер|саябақ", "парк")]


def explicit_cats(text: str) -> list[str]:
    t = (text or "").lower()
    out = [c for rx, c in EXPLICIT if re.search(rx, t)]
    if "бар" in out and re.search(r"[әіңғүұқөһ]", t) and not re.search(r"\bbars?\b|паб|pub|\bбарға\b|\bбарына\b", t):
        out.remove("бар")  # по-казахски «бар» = «есть» («бір сағат бар»), это не бар
    return out


def wanted_cats(text: str, parsed: dict) -> list[str]:
    """Тип места, который человек хочет посетить — без слов из «где я» и «куда мне»: «я у мечети Нур Астана» — не просьба в мечеть."""
    where = " ".join(str(parsed.get(k) or "") for k in ("origin", "destination")).lower()
    return [c for c in explicit_cats(text) if not any(re.search(rx, where) for rx, cc in EXPLICIT if cc == c)]


KEY_NOTE = re.compile(r"Гололёд|Туман|Ощущается|Вылет|Поезд|не назвал время|Такси или пешком|Выходишь в|выходной|без такси|не нашёл|Воздух|лучше прийти|Лучший вид|Просил|казахская кухня|Вместо такси")
# события: за сколько прийти заранее и почему
EVENT_BUFFER = {"concert": (25, "Концерт", "досмотр и очередь на вход"), "match": (30, "Матч", "досмотр на стадионе"),
                "theatre": (20, "Спектакль", "гардероб, после третьего звонка не пускают"),
                "cinema": (10, "Сеанс", "билеты и попкорн"), "meeting": (5, "Встреча", "небольшой запас")}


# названия других городов: «я в Москве» — это не БЦ «Москва» в Астане, а другой город
OTHER_CITIES = re.compile(r"^(в |in )?(москв\w*|moscow|алмат\w*|almaty|шымкент\w*|shymkent|караганд\w*|karaganda|"
                          r"санкт-петербург\w*|питер\w*|ташкент\w*|tashkent|бишкек\w*|bishkek|дубай|dubai|стамбул\w*|istanbul|"
                          r"лондон\w*|london|париж\w*|paris|нью-йорк\w*|new york)$", re.I)
HOME = re.compile(r"^(дом|домой|до дома|home|үй|үйге|квартир\w*)$", re.I)
DATA_DATE = datetime.fromtimestamp((ROOT / "data" / "poi.json").stat().st_mtime).strftime("%d.%m.%Y")


def _now(s: str | None) -> datetime:
    now = datetime.now(KZ).replace(tzinfo=None, second=0, microsecond=0)
    if s:
        h, m = map(int, s.split(":"))
        now = now.replace(hour=h, minute=m)
    return now


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/city")
def city_page():
    """Спрос для города: обезличенная статистика (только счётчики, без координат и текстов запросов)."""
    return FileResponse(ROOT / "static" / "city.html")


@app.get("/api/city")
def city_stats():
    return citystats.snapshot()


@app.get("/api/meta")
def meta():
    w = geo.weather()
    cats = {}
    for p in planner.POIS:
        cats[p["category"]] = cats.get(p["category"], 0) + 1
    return {"weather": {k: v for k, v in w.items() if k != "_hourly"}, "cold": geo.cold_level(w), "pois": len(planner.POIS),
            "with_hours": sum(1 for p in planner.POIS if "opening_hours" in p["tags"]),
            "categories": cats, "landmarks": geo.LANDMARKS, "now": _now(None).strftime("%H:%M")}


@app.get("/api/where")
def where(lat: float, lon: float, lang: str = "ru"):
    """Адрес текущей точки пользователя (улица, дом). Координаты не сохраняются и не пишутся в лог."""
    return {"label": geo.reverse(round(lat, 4), round(lon, 4), lang if lang in ("ru", "en", "kk") else "ru")}


@app.get("/api/place")
def place(lat: float, lon: float, lang: str = "ru"):
    """Карточка точки, куда ткнули на карте: адрес + ближайшее место из нашей базы (в пределах ~60 м).
    Большие здания-ориентиры (Хан Шатыр, ТРЦ, арены) важнее кафе внутри них — если ткнули в такое здание, показываем его."""
    best, bd = None, 0.06
    for p in planner.POIS:
        if abs(p["lat"] - lat) > 0.001 or abs(p["lon"] - lon) > 0.0015:
            continue
        d = geo.haversine_km((lat, lon), (p["lat"], p["lon"]))
        if d < bd:
            best, bd = p, d
    SMALL = ("кафе", "ресторан", "фастфуд", "бар", "детский центр", "развлечения", "кино", "книжный")
    if best and best["category"] in SMALL:   # кафе внутри ТРЦ → сам ТРЦ, если он рядом (центр здания бывает в 100–150 м)
        mall = min(((geo.haversine_km((lat, lon), (p["lat"], p["lon"])), p) for p in planner.POIS
                    if p["category"] in ("торговый центр", "музей", "театр", "стадион")
                    and abs(p["lat"] - lat) < 0.002 and abs(p["lon"] - lon) < 0.003), key=lambda x: x[0], default=(9, None))
        if mall[1] and mall[0] < 0.15:
            best, bd = mall[1], mall[0]
    big = min(((geo.haversine_km((lat, lon), (l["lat"], l["lon"])), l) for l in geo.LANDMARKS), key=lambda x: x[0], default=(9, None))
    if big[1] and big[0] < 0.12 and (not best or best["category"] in ("кафе", "ресторан", "фастфуд", "бар", "детский центр", "развлечения")):
        lm = big[1]
        twin = next((p for p in planner.POIS if geo.haversine_km((lm["lat"], lm["lon"]), (p["lat"], p["lon"])) < 0.3
                     and (p["name"].lower() in {n.lower() for n in [lm["name"], *lm.get("aliases", [])]})), None)
        best = twin or {"name": lm["name"], "name_en": lm.get("name_en"), "name_kk": lm.get("name_kk"), "category": "ориентир",
                        "lat": lm["lat"], "lon": lm["lon"], "tags": {}}
        if not best.get("name_en"):
            best = {**best, "name_en": lm.get("name_en"), "name_kk": lm.get("name_kk")}
        bd = big[0]
    poi = None
    if best:
        dg = best.get("dgis") or {}
        poi = {"name": best["name"], "name_en": best.get("name_en"), "name_kk": best.get("name_kk"), "category": best["category"], "rating": dg.get("rating"), "reviews": dg.get("reviews"),
               "hours": best["tags"].get("opening_hours"), "dgis_id": dg.get("id"), "wiki": (best.get("wiki") or {}).get("article"),
               "lat": best["lat"], "lon": best["lon"], "m": round(bd * 1000), "image": planner.image_of(best),
               **planner.contacts(best.get("tags") or {})}
    return {"address": geo.reverse_cached(round(lat, 4), round(lon, 4), lang if lang in ("ru", "en", "kk") else "ru"), "poi": poi}


class TrReq(BaseModel):
    texts: list[str] = []
    lang: str = "en"


@app.post("/api/translate")
def translate(req: TrReq, request: Request):
    """Перевод пояснений «Данные и оговорки» — только когда человек открыл «Как посчитано» не на русском."""
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    texts = [t[:400] for t in req.texts[:16]]
    if req.lang not in ("en", "kk") or not texts or (ip not in ("127.0.0.1", "::1") and _too_many(ip)):
        return {"texts": texts}
    return {"texts": llm.translate(texts, req.lang, max_tokens=1600)}


@app.get("/api/lrt")
def lrt_map():
    """Линия LRT и станции для карты (OSM, scripts/collect_city.py)."""
    f = Path(__file__).resolve().parent.parent / "data" / "lrt.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"stations": [], "geometry": []}


@app.get("/api/pois")
def pois():
    """Все места базы — лёгкая версия для точек на карте."""
    return [[p["lon"], p["lat"], p["category"]] for p in planner.POIS]


_hits: dict[str, list[float]] = {}


def _too_many(ip: str, limit=60, per=600) -> bool:
    """Не больше 60 маршрутов за 10 минут с одного адреса — чтобы публичное демо не сожгло ключи."""
    now = time.time()
    h = [t for t in _hits.get(ip, []) if now - t < per]
    h.append(now)
    _hits[ip] = h
    return len(h) > limit


def mode_advice(steps: list, now: datetime, end: str = "финиш") -> str:
    """Человек спросил «такси или пешком?» — по каждому отрезку: сколько пешком и сколько на такси (оценка)."""
    names = [s.get("name") for s in steps if s["type"] == "stop"] + [end]
    parts, i = [], 0
    pos = None
    for s in steps:
        if s["type"] == "leg":
            to = names[i] if i < len(names) else "финиш"
            if s["mode"] == "walk" and s["min"] >= 20:
                c = s["coords"]
                taxi = round(geo.car_min_est((c[0][1], c[0][0]), (c[-1][1], c[-1][0]), now) + geo.TAXI_WAIT_MIN)
                parts.append(f"до «{to}» пешком {s['min']} мин, на такси ~{taxi} мин с подачей (оценка) — "
                             + ("тут лучше такси." if s["min"] - taxi >= 10 else "можно и пешком."))
            elif s["mode"] == "walk":
                parts.append(f"до «{to}» {s['min']} мин пешком — такси не нужно.")
            else:
                parts.append(f"до «{to}» далеко — такси ~{s['min']} мин.")
        elif s["type"] == "stop":
            i += 1
    return "Такси или пешком: " + " ".join(parts)


def start_offset(text: str) -> int:
    """«Через полчаса выйду», «выезжаю через 20 минут», «in an hour I'll leave» → сколько минут до выхода."""
    t = text.lower()
    if not re.search(r"выйд|выхож|выед|выезж|выдвин|пойду|поеду|leave|head out|шығам|шығамын", t):
        return 0
    m = re.search(r"через\s+(пол\s?часа|полчаса|час(?:ик)?|(\d+)\s*(мин|час)\w*)|in\s+(half an hour|an hour|(\d+)\s*(min|hour)\w*)|"
                  r"(\d+)\s*(минуттан|сағаттан)", t)
    if not m:
        return 0
    s = m.group(0)
    if "пол" in s or "half" in s:
        return 30
    n = re.search(r"\d+", s)
    k = int(n.group()) if n else 1
    return min(k * 60 if re.search(r"час|hour|сағат", s) else k, 12 * 60)


GENERIC_STOPS = [   # «зайти в библиотеку» без названия — ближайшая такого типа
    (r"(?:в |на )?(?:библиотек\w*|кітапхана\w*|library)", "библиотека"),
    (r"(?:в |на )?(?:кофейн\w*|кафе|coffee(?: shop)?|cafe|кофеханa?\w*)", "кафе"),
    (r"(?:в |на )?(?:коворкинг\w*|coworking)", "коворкинг"),
    (r"(?:в |на )?(?:книжн\w*(?: магазин\w*)?|bookstore|кітап дүкен\w*)", "книжный"),
    (r"(?:в |на )?(?:тц|трц|торгов\w* центр\w*|mall|сауда орталығ\w*)", "торговый центр"),
]


@app.post("/api/plan")
def make_plan(req: PlanReq, request: Request):
    try:
        r = _make_plan(req, request)
        if r.get("error") and "weather" not in r:   # отказ ещё до планирования (не понял, где ты / другой город)
            citystats.refused((r.get("parsed") or {}).get("language"))
        return r
    except Exception:
        import traceback
        traceback.print_exc()
        return {"error": "Что-то пошло не так при расчёте. Попробуй ещё раз или переформулируй запрос — например «я у Байтерека, есть час»."}


def _make_plan(req: PlanReq, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if ip not in ("127.0.0.1", "::1") and _too_many(ip):
        return {"error": "Слишком много запросов подряд — подожди пару минут. Это защита демо от перерасхода."}
    timings = {}
    t0 = time.time()
    now = _now(req.now)
    has_point = bool(req.point and req.point.get("lat") is not None)
    parsed = req.parsed or llm.understand(req.text, now, has_point, list(req.saved))
    timings["understand"] = round((time.time() - t0) * 1000)
    if not parsed.get("via") and re.search(r"\bпотом\b|\bзатем\b|после этого|\bthen\b|\bкейін\b|\bсосын\b", (req.text or "").lower()):
        # ИИ не разобрал список пунктов — достаём ориентиры из текста по порядку (кроме места старта)
        names = geo.landmarks_in_order(req.text)
        start = geo.find_place(parsed.get("origin") or "", planner.POIS, req.saved) if parsed.get("origin") else None
        names = [n for n in names if not (start and n == start["label"])]
        end = parsed.get("destination")
        if end and names and geo.find_place(end, planner.POIS, req.saved) and geo.find_place(end, planner.POIS, req.saved)["label"] == names[-1]:
            names = names[:-1]
        if names and (len(names) >= 2 or end):
            parsed = {**parsed, "via": [{"place": n, "stay_min": None} for n in names]}
    later = start_offset(req.text or "")
    if later:
        now = now + timedelta(minutes=later)   # выходишь не сейчас — часы работы и пробки считаем на время выхода
    at_home = bool(re.search(r"\b(я|мы) (сейчас )?дома\b|\bиз дома\b|\bс дома\b|\bот дома\b|\bиз дому\b|\bat home\b|үйдемін|үйден",
                             (req.text or "").lower()))

    # «домой» = закладка со значком «Дом» (как бы она ни называлась: «Здесь мой дом», «Квартира»…)
    home = next((k for k, v in req.saved.items() if isinstance(v, dict) and v.get("kind") == "home"), None) or \
        next((k for k in req.saved if HOME.match(k) or re.search(r"\bдом\b|\bhome\b|\bүй", k.lower())), None)
    t1 = time.time()
    origin = None
    # моё GPS-место — по умолчанию; но если человек сам написал «я у Байтерека», верим тексту
    if req.point and req.point.get("mine") and parsed.get("origin") and not OTHER_CITIES.match(parsed["origin"].strip()):
        origin = geo.find_place(parsed["origin"], planner.POIS, req.saved)
    if origin:
        pass
    elif req.point and req.point.get("lat") is not None:
        lat, lon = geo.round_point(req.point["lat"], req.point["lon"])
        pl = str(req.point.get("label") or "")[:60].strip()   # «Отсюда» с карточки места — его название
        origin = {"lat": lat, "lon": lon, "label": "Ты здесь (±150 м)" if req.point.get("mine") else (pl or "Точка на карте (±150 м)"),
                  "source": "карта, округлено"}
    elif OTHER_CITIES.match((parsed.get("origin") or "").strip()):
        origin = None
    else:
        origin = geo.find_place(parsed.get("origin") or "", planner.POIS, req.saved)
    if home and (at_home or (parsed.get("origin") and HOME.match(str(parsed["origin"]).strip()))) and \
            not (req.point and req.point.get("mine")) and (not origin or not re.search(r"\d", parsed.get("origin") or "")):
        origin = geo.find_place(home, planner.POIS, req.saved)   # «из дома» — это закладка «Дом», а не «Дом на Ботаническом»
    lang = parsed.get("language")
    if not origin and parsed.get("via"):
        first = parsed["via"][0]
        first = first.get("place") if isinstance(first, dict) else str(first)
        origin = geo.find_place(first or "", planner.POIS, req.saved)   # первый пункт остаётся в списке — там и побудешь
        from_first = bool(origin)
    if not origin and parsed.get("destination") and not parsed.get("deadline") and             re.search(r"рядом|около|возле|поблизости|недалеко|nearby|near|around|жаны|маңы", (req.text or "").lower()):
        # «еду с ребёнком к ЭКСПО — что там рядом?» — считаем от того места, куда человек едет
        origin = geo.find_place(parsed["destination"], planner.POIS, req.saved)
        if origin:
            parsed = {**parsed, "origin": parsed["destination"], "destination": None, "return_to_origin": True}
    if not origin:
        other = OTHER_CITIES.match((parsed.get("origin") or "").strip())
        msg = ("Похоже, ты не в Астане. «Окно» пока работает только по Астане — напиши ориентир в городе "
               "(«я у Байтерека») или ткни в карту." if other else
               f"Не нашёл «{parsed['origin']}» в Астане. «Окно» пока работает только по Астане — напиши ориентир в городе "
               "(«я у Байтерека») или ткни в карту." if parsed.get("origin") else
               "Не понял, где ты сейчас. Напиши ориентир («я у Байтерека») или ткни в карту.")
        return {"error": llm.translate([msg], lang)[0], "parsed": parsed}
    dest = None
    for key in ("origin", "destination"):
        if home and parsed.get(key) and HOME.match(str(parsed[key]).strip()):
            parsed[key] = home
    if parsed.get("destination") and not parsed.get("return_to_origin"):
        if HOME.match(parsed["destination"].strip()) and not home:
            no_home = not at_home   # «я дома … потом обратно домой» — дом и есть точка старта, подсказка не нужна   # дом не сохранён: не отказываем — помогаем с остальным и подсказываем, как сохранить дом
            parsed = {**parsed, "destination": None, "return_to_origin": True}
        dest = None if not parsed.get("destination") else None if OTHER_CITIES.match(parsed["destination"].strip()) else \
            geo.find_place(parsed["destination"], planner.POIS, req.saved)
        if not dest and parsed.get("destination") and not parsed.get("via"):
            return {"error": llm.translate([f"Не нашёл «{parsed['destination']}» в Астане. Попробуй назвать иначе."], lang)[0],
                    "parsed": parsed}
    # «сначала X, потом Y, в конце Z» — человек сам задал пункты: строим цепочку в его порядке
    via, skipped = [], []
    for v in parsed.get("via") or []:
        name = (v.get("place") if isinstance(v, dict) else str(v)) or ""
        if home and HOME.match(name.strip()):
            name = home
        gen = next((c for rx, c in GENERIC_STOPS if re.fullmatch(rx, name.strip().lower())), None)
        if gen:
            prev = via[-1] if via else origin
            kids = bool(re.search(r"дет|ребен|ребён|бала|kid|child", (req.text or "").lower()))
            cands = [p for p in planner.POIS if p["category"] == gen and
                     (kids or not re.search(r"детск|балалар|children", p["name"].lower()))]
            if prev and cands:
                p = min(cands, key=lambda p: geo.haversine_km((prev["lat"], prev["lon"]), (p["lat"], p["lon"])))
                pt = {"lat": p["lat"], "lon": p["lon"], "label": p["name"], "source": "ближайшее место этого типа"}
            else:
                pt = None
        else:
            pt = geo.find_place(name, planner.POIS, req.saved) if name.strip() else None
        if not pt:
            skipped.append(name)   # «на пары», «в магазин» — не место на карте: пропускаем, а не отказываем
            continue
        stay = v.get("stay_min") if isinstance(v, dict) else None
        via.append({**pt, "stay": int(stay) if isinstance(stay, (int, float)) and 0 < stay <= 600 else None})
    keep = list(req.keep)
    if locals().get("from_first") and len(via) == 1 and not dest:
        # «сначала в музей, потом что-то ещё»: музей — обязательная остановка, остальное подберём сами
        near = min(planner.POIS, key=lambda p: geo.haversine_km((via[0]["lat"], via[0]["lon"]), (p["lat"], p["lon"])))
        if geo.haversine_km((via[0]["lat"], via[0]["lon"]), (near["lat"], near["lon"])) < 0.3:
            first_poi = {**near, "_stay": via[0].get("stay")}
            via = []
    m = re.search(r"\b(?:к|до|by|at)\s+(\d{1,2})(?:[:.](\d{2}))?", (req.text or "").lower())
    if m and via and parsed.get("deadline"):
        before = (req.text or "")[:m.start()].lower()
        names = geo.landmarks_in_order(before[-40:])
        hit = next((v for v in via if names and v["label"] == names[-1]), None)
        if hit and via.index(hit) < len(via) - 1 + (1 if dest else 0) and not re.search(r"рейс|вылет|flight|поезд", (req.text or "").lower()):
            hit["by"] = f"{int(m.group(1)):02d}:{m.group(2) or '00'}"
            parsed = {**parsed, "deadline": None}   # срок — к этому пункту; в конце маршрута — когда получится
    if not dest and via and not parsed.get("return_to_origin"):
        dest = via.pop()   # конечная точка не нашлась — последний названный пункт и есть конец маршрута
    if at_home and origin and origin.get("source") not in ("твоё сохранённое место", "справочник ориентиров") and \
            not (req.point and req.point.get("mine")):
        origin = {**origin, "label": f"Дом · {origin['label']}"}
    destination = dest or {**origin, "label": "домой" if at_home else f"обратно: {origin['label']}"}
    if not parsed.get("deadline") and not parsed.get("duration_min") and \
            set(explicit_cats(req.text)) & {"боулинг", "кино", "театр", "каток", "музей"}:
        parsed = {**parsed, "duration_min": 150}  # долгое занятие без названного времени — окно шире, чем 90 мин
        parsed["_default_window"] = True
    if dest and "Аэропорт" in dest["label"] and not parsed.get("deadline") and parsed.get("duration_min") and \
            re.search(r"вылет|рейс|flight|ұшақ|самолет|самолёт", (req.text or "").lower()):
        parsed = {**parsed, "deadline": (now + timedelta(minutes=parsed["duration_min"])).strftime("%H:%M")}
    deadline, window_label = llm.resolve_times(parsed, now)
    if dest and not parsed.get("deadline") and parsed.get("duration_min") and parsed.get("event") == "meeting" and "work" in (parsed.get("prefs") or []):
        trip = geo.car_min_est((origin["lat"], origin["lon"]), (dest["lat"], dest["lon"]), now) + geo.TAXI_WAIT_MIN
        deadline = now + timedelta(minutes=parsed["duration_min"] * 1.25 + trip + 15)
    travel_note = None
    end = dest or origin  # «прилетел, рейс дальше в 14:00» — петля из аэропорта, но правило «за 1,5 часа» то же
    if end and parsed.get("deadline"):
        if "Аэропорт" in end["label"]:
            trip = geo.car_min_est((origin["lat"], origin["lon"]), (end["lat"], end["lon"]), now) + geo.TAXI_WAIT_MIN
            if now + timedelta(minutes=trip + 5) > deadline - timedelta(minutes=90) and end is not origin:
                deadline -= timedelta(minutes=60)   # за 1,5 часа уже не успеть — честно считаем «впритык», а не отказываем
                travel_note = (f"Вылет в {parsed['deadline']}: за 1,5 часа до вылета в аэропорт уже не успеть (такси ~{round(trip)} мин) — "
                               f"считаю впритык, к {deadline:%H:%M}. Для внутреннего рейса это нормально, для международного — рискованно.")
            else:
                deadline -= timedelta(minutes=90)
                travel_note = f"Вылет в {parsed['deadline']}: в аэропорту нужно быть за 1,5 часа — к {deadline:%H:%M}."
        elif "Вокзал" in end["label"]:
            deadline -= timedelta(minutes=20)
            travel_note = f"Поезд в {parsed['deadline']}: на вокзале лучше быть за 20 минут — к {deadline:%H:%M}."
        elif parsed.get("event") in EVENT_BUFFER and not HOME.match((parsed.get("destination") or "").strip()):
            m, what, why = EVENT_BUFFER[parsed["event"]]
            deadline -= timedelta(minutes=m)
            travel_note = f"{what} в {parsed['deadline']}: лучше прийти за {m} минут ({why}) — к {deadline:%H:%M}."
    if not parsed.get("via") and (parsed.get("_default_window") or (not parsed.get("deadline") and not parsed.get("duration_min"))):
        mins = parsed.get("duration_min") or 90
        travel_note = (travel_note + " " if travel_note else "") + \
            f"Ты не назвал время — взял окно {mins} минут. Напиши «к 19:00» или «есть 40 минут», чтобы точнее."
    if parsed.get("deadline") and parsed.get("duration_min") and not travel_note:
        deadline = min(deadline, now + timedelta(minutes=parsed["duration_min"]))
    if later:
        travel_note = f"Выходишь в {now:%H:%M} — всё посчитано от этого времени. " + (travel_note or "")
    if locals().get("no_home"):
        travel_note = (travel_note + " " if travel_note else "") + \
            "Где твой дом, я не знаю — маршрут заканчивается здесь. Нажми на свой дом на карте → «В закладки» → значок «Дом», и «домой» заработает."
    if skipped:
        travel_note = (travel_note + " " if travel_note else "") + f"«{', '.join(skipped)}» не нашёл на карте — пропустил этот пункт."
    timings["geocode"] = round((time.time() - t1) * 1000)

    said = parsed.get("temp_c")
    if req.weather == "cold":
        w = geo.weather({"apparent": -25, "wind": 30})
    elif isinstance(said, (int, float)) and said <= -10:
        w = geo.weather({"apparent": said, "wind": 20})  # человек сам написал, какой мороз
        w["source"] = f"со слов пользователя: {said:+.0f}°"
    else:
        w = geo.weather_window(now, deadline)   # погода на часы маршрута (прогноз), а не только «сейчас»
    haz = w.get("hazards") or []
    if "ice" in haz:
        travel_note = (travel_note + " " if travel_note else "") + \
            "Гололёд возможен: около нуля и осадки — иди осторожно, время пешком посчитано с запасом."
    if "fog" in haz:
        travel_note = (travel_note + " " if travel_note else "") + \
            "Туман: видимость плохая — вид с высоты и виды на город сейчас не лучшие."
    intent = {"origin": origin, "destination": destination, "deadline": deadline.isoformat(),
              "prefs": parsed.get("prefs"), "with_kids": parsed.get("with_kids"),
              "mobility": parsed.get("mobility"), "transport": parsed.get("transport"), "group_size": parsed.get("group_size"),
              "language": parsed.get("language"), "explicit": wanted_cats(req.text, parsed),
              "dinner": bool(re.search(r"ужин|поужина|dinner|кешкі ас", (req.text or "").lower())),
              "kazakh_food": bool(re.search(r"национальн\w* (?:кухн|ед|блюд|тағам)|казахск\w* (?:кухн|ед|блюд)|местн\w* (?:еду|кухн|блюд)|традицион|qazaq|kazakh|national food|local food|"
                                            r"traditional|ұлттық тағам|қазақ тағам|қазақша тағам|бешбармак|beshbarmak|кымыз|қымыз|kumis|kymyz", req.text.lower()))}
    t2 = time.time()
    if via:
        intent["via"] = via
        raw = llm.resolve_times(parsed, now)[0] if parsed.get("deadline") else None   # срок без запаса на вход/регистрацию
        result = planner.plan_chain(intent, now, w, has_deadline=bool(parsed.get("deadline")), hard_deadline=raw)
        if result.get("summary") and not parsed.get("deadline"):
            deadline = now + timedelta(minutes=result["summary"]["window_min"])   # срока нет — шкала до конца маршрута
    else:
        if locals().get("first_poi"):
            result = planner.plan_after_first(intent, now, w, first_poi)
        else:
            result = planner.plan(intent, now, w, exclude=req.exclude, required=keep)
    timings["plan"] = round((time.time() - t2) * 1000)
    # афиша: событие сегодня, которое целиком помещается в окно; спросил про театр/концерт — ближайшие события
    A, Bp = (origin["lat"], origin["lon"]), (destination["lat"], destination["lon"])
    evs = events.fitting(A, Bp, now, deadline) or (events.upcoming(A, now) if events.WANTS.search(req.text or "") else [])
    base = {"parsed": parsed, "origin": origin, "destination": destination, "now": now.strftime("%H:%M"), "events": evs,
            "deadline": deadline.strftime("%H:%M"), "window_label": window_label,
            "weather": {k: v for k, v in w.items() if k != "_hourly"}}
    if travel_note:
        result.setdefault("notes", []).insert(0, travel_note)
    if not result.get("error") and re.search(r"такси|taxi|пешк|walk|жаяу", (req.text or "").lower()):
        result.setdefault("notes", []).append(mode_advice(result["steps"], now, destination["label"]))
    if not result.get("error"):
        tip = lrt.advice(result["steps"], destination["label"], (result.get("summary") or {}).get("buffer_min") or 0)   # длинный отрезок на такси, а вдоль него LRT
        if tip:
            result.setdefault("notes", []).append(tip)
    citystats.record(parsed, result, req.weather == "cold", len(evs), now.hour)   # обезличенные счётчики для города
    if result.get("error"):
        tr = llm.translate([result["error"], *(result.get("notes") or [])], lang)
        result["error"], result["notes"] = tr[0], tr[1:]
        return {**base, **result, "timings": timings}
    t3 = time.time()
    # какие заметки важные (показываем сразу, остальные — в «как посчитано»); отмечаем до перевода на язык запроса
    result["notes_key"] = [bool(KEY_NOTE.search(n)) for n in result.get("notes") or []]
    result = llm.explain(req.text, intent, result, w)
    timings["explain"] = round((time.time() - t3) * 1000)
    return {**base, **result, "timings": timings,
            "data_used": [
                f"Твоя точка: {origin['label']} — {origin['source']}; координаты не сохраняются.",
                f"Места: база OpenStreetMap (обновлена {DATA_DATE}) + Википедия + 2GIS, {len(planner.POIS)} мест Астаны; рассмотрено {result['considered']['candidates']} кандидатов, "
                f"{result['considered']['plans']} вариантов маршрута.",
                "Время пешком: реальный маршрут по улицам (OSRM/FOSSGIS), шаг 4,5 км/ч.",
                "Часы работы: проверены на момент твоего прихода и ухода; нет в данных — «оценка».",
                "Уточнение по 2GIS (Places API): свежие часы работы, рейтинг и отметки вроде «можно с ноутбуком» — "
                "только для мест-кандидатов; помечено «2GIS».",
                f"Погода: {w['source']}.",
                "Тексты «почему» и «что делать»: Gemini 3.5 Flash (Google), только по фактам выше. Места выбирает алгоритм, не ИИ.",
                (f"Афиша: официальный сайт Astana Opera (обновлено {events.UPDATED}) — "
                 + ("в это окно ничего не помещается, показаны ближайшие события по твоему запросу." if evs and evs[0].get("later")
                    else "событие показано, потому что целиком помещается в окно." if evs
                    else "событий, которые помещаются в это окно, нет.")),
                "Приватность: геопозиция округляется до ~150 м; адрес «где я» — OpenStreetMap (Nominatim); закладки и последняя точка "
                "живут только в твоём браузере. Голосовой ввод распознаёт сам браузер (в Chrome — сервис Google). Аккаунтов и истории нет.",
            ]}
