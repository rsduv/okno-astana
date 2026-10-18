"""Сервер «Окна». Запуск: python -m uvicorn app.main:app --port 8000  →  http://localhost:8000"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import citystats, geo, llm, planner, events

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


KEY_NOTE = re.compile(r"Гололёд|Туман|Ощущается|Вылет|Поезд|не назвал|без такси|не нашёл|Воздух|лучше прийти|Лучший вид|Просил|казахская кухня")
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
    return {"label": geo.reverse(round(lat, 4), round(lon, 4), lang)}


@app.get("/api/place")
def place(lat: float, lon: float, lang: str = "ru"):
    """Карточка точки, куда ткнули на карте: адрес + ближайшее место из нашей базы (в пределах ~60 м)."""
    best, bd = None, 0.06
    for p in planner.POIS:
        if abs(p["lat"] - lat) > 0.001 or abs(p["lon"] - lon) > 0.0015:
            continue
        d = geo.haversine_km((lat, lon), (p["lat"], p["lon"]))
        if d < bd:
            best, bd = p, d
    poi = None
    if best:
        dg = best.get("dgis") or {}
        poi = {"name": best["name"], "name_en": best.get("name_en"), "name_kk": best.get("name_kk"), "category": best["category"], "rating": dg.get("rating"), "reviews": dg.get("reviews"),
               "hours": best["tags"].get("opening_hours"), "dgis_id": dg.get("id"), "wiki": (best.get("wiki") or {}).get("article"),
               "lat": best["lat"], "lon": best["lon"], "m": round(bd * 1000), "image": (best.get("wiki") or {}).get("image"),
               **planner.contacts(best.get("tags") or {})}
    return {"address": geo.reverse(round(lat, 4), round(lon, 4), lang), "poi": poi}


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
    lang = parsed.get("language")
    if not origin:
        msg = (f"Не нашёл «{parsed['origin']}» в Астане. «Окно» пока работает только по Астане — напиши ориентир в городе "
               "(«я у Байтерека») или ткни в карту." if parsed.get("origin") else
               "Не понял, где ты сейчас. Напиши ориентир («я у Байтерека») или ткни в карту.")
        return {"error": llm.translate([msg], lang)[0], "parsed": parsed}
    dest = None
    if parsed.get("destination") and not parsed.get("return_to_origin"):
        if HOME.match(parsed["destination"].strip()) and not any(HOME.match(k) for k in req.saved):
            return {"error": llm.translate(["Не знаю, где твой дом. Отметь его на карте и нажми «сохранить точку» → «Дом» — "
                                            "тогда «домой к 23:00» будет работать. Точка хранится только в твоём браузере."], lang)[0],
                    "parsed": parsed}
        dest = None if OTHER_CITIES.match(parsed["destination"].strip()) else \
            geo.find_place(parsed["destination"], planner.POIS, req.saved)
        if not dest:
            return {"error": llm.translate([f"Не нашёл «{parsed['destination']}» в Астане. Попробуй назвать иначе."], lang)[0],
                    "parsed": parsed}
    destination = dest or {**origin, "label": f"обратно: {origin['label']}"}
    if not parsed.get("deadline") and not parsed.get("duration_min") and \
            set(explicit_cats(req.text)) & {"боулинг", "кино", "театр", "каток", "музей"}:
        parsed = {**parsed, "duration_min": 150}  # долгое занятие без названного времени — окно шире, чем 90 мин
        parsed["_default_window"] = True
    deadline, window_label = llm.resolve_times(parsed, now)
    travel_note = None
    end = dest or origin  # «прилетел, рейс дальше в 14:00» — петля из аэропорта, но правило «за 1,5 часа» то же
    if end and parsed.get("deadline"):
        if "Аэропорт" in end["label"]:
            deadline -= timedelta(minutes=90)
            travel_note = f"Вылет в {parsed['deadline']}: в аэропорту нужно быть за 1,5 часа — к {deadline:%H:%M}."
        elif "Вокзал" in end["label"]:
            deadline -= timedelta(minutes=20)
            travel_note = f"Поезд в {parsed['deadline']}: на вокзале лучше быть за 20 минут — к {deadline:%H:%M}."
        elif parsed.get("event") in EVENT_BUFFER and not HOME.match((parsed.get("destination") or "").strip()):
            m, what, why = EVENT_BUFFER[parsed["event"]]
            deadline -= timedelta(minutes=m)
            travel_note = f"{what} в {parsed['deadline']}: лучше прийти за {m} минут ({why}) — к {deadline:%H:%M}."
    if parsed.get("_default_window") or (not parsed.get("deadline") and not parsed.get("duration_min")):
        mins = parsed.get("duration_min") or 90
        travel_note = (travel_note + " " if travel_note else "") + \
            f"Ты не назвал время — взял окно {mins} минут. Напиши «к 19:00» или «есть 40 минут», чтобы точнее."
    if parsed.get("deadline") and parsed.get("duration_min") and not travel_note:
        deadline = min(deadline, now + timedelta(minutes=parsed["duration_min"]))
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
              "kazakh_food": bool(re.search(r"национальн|казахск|qazaq|kazakh|ұлттық|қазақ", req.text.lower()))}
    t2 = time.time()
    result = planner.plan(intent, now, w, exclude=req.exclude, required=req.keep)
    timings["plan"] = round((time.time() - t2) * 1000)
    # афиша: событие сегодня, которое целиком помещается в окно; спросил про театр/концерт — ближайшие события
    A, Bp = (origin["lat"], origin["lon"]), (destination["lat"], destination["lon"])
    evs = events.fitting(A, Bp, now, deadline) or (events.upcoming(A, now) if events.WANTS.search(req.text or "") else [])
    base = {"parsed": parsed, "origin": origin, "destination": destination, "now": now.strftime("%H:%M"), "events": evs,
            "deadline": deadline.strftime("%H:%M"), "window_label": window_label,
            "weather": {k: v for k, v in w.items() if k != "_hourly"}}
    if travel_note:
        result.setdefault("notes", []).insert(0, travel_note)
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
