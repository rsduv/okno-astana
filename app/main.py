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

from . import geo, llm, planner

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


KEY_NOTE = re.compile(r"Ощущается|Вылет|Поезд|не назвал|без такси|не нашёл|Воздух|лучше прийти")
# события: за сколько прийти заранее и почему
EVENT_BUFFER = {"concert": (25, "Концерт", "досмотр и очередь на вход"), "match": (30, "Матч", "досмотр на стадионе"),
                "theatre": (20, "Спектакль", "гардероб, после третьего звонка не пускают"),
                "cinema": (10, "Сеанс", "билеты и попкорн"), "meeting": (5, "Встреча", "небольшой запас")}


def _now(s: str | None) -> datetime:
    now = datetime.now(KZ).replace(tzinfo=None, second=0, microsecond=0)
    if s:
        h, m = map(int, s.split(":"))
        now = now.replace(hour=h, minute=m)
    return now


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/meta")
def meta():
    w = geo.weather()
    cats = {}
    for p in planner.POIS:
        cats[p["category"]] = cats.get(p["category"], 0) + 1
    return {"weather": w, "cold": geo.cold_level(w), "pois": len(planner.POIS),
            "with_hours": sum(1 for p in planner.POIS if "opening_hours" in p["tags"]),
            "categories": cats, "landmarks": geo.LANDMARKS, "now": _now(None).strftime("%H:%M")}


@app.get("/api/pois")
def pois():
    """Все места базы — лёгкая версия для точек на карте."""
    return [[p["lon"], p["lat"], p["category"]] for p in planner.POIS]


_hits: dict[str, list[float]] = {}


def _too_many(ip: str, limit=25, per=600) -> bool:
    """Не больше 25 маршрутов за 10 минут с одного адреса — чтобы публичное демо не сожгло ключи."""
    now = time.time()
    h = [t for t in _hits.get(ip, []) if now - t < per]
    h.append(now)
    _hits[ip] = h
    return len(h) > limit


@app.post("/api/plan")
def make_plan(req: PlanReq, request: Request):
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
    if req.point and req.point.get("lat") is not None:
        lat, lon = geo.round_point(req.point["lat"], req.point["lon"])
        origin = {"lat": lat, "lon": lon, "label": "Ты здесь (±150 м)" if req.point.get("mine") else "Точка на карте (±150 м)", "source": "карта, округлено"}
    else:
        origin = geo.find_place(parsed.get("origin") or "", planner.POIS, req.saved)
    lang = parsed.get("language")
    if not origin:
        return {"error": llm.translate(["Не понял, где ты сейчас. Напиши ориентир («я у Байтерека») или ткни в карту."], lang)[0],
                "parsed": parsed}
    dest = None
    if parsed.get("destination") and not parsed.get("return_to_origin"):
        dest = geo.find_place(parsed["destination"], planner.POIS, req.saved)
        if not dest:
            return {"error": llm.translate([f"Не нашёл «{parsed['destination']}» в Астане. Попробуй назвать иначе."], lang)[0],
                    "parsed": parsed}
    destination = dest or {**origin, "label": f"обратно: {origin['label']}"}
    deadline, window_label = llm.resolve_times(parsed, now)
    travel_note = None
    if dest and parsed.get("deadline"):
        if "Аэропорт" in dest["label"]:
            deadline -= timedelta(minutes=90)
            travel_note = f"Вылет в {parsed['deadline']}: в аэропорту нужно быть за 1,5 часа — к {deadline:%H:%M}."
        elif "Вокзал" in dest["label"]:
            deadline -= timedelta(minutes=20)
            travel_note = f"Поезд в {parsed['deadline']}: на вокзале лучше быть за 20 минут — к {deadline:%H:%M}."
        elif parsed.get("event") in EVENT_BUFFER:
            m, what, why = EVENT_BUFFER[parsed["event"]]
            deadline -= timedelta(minutes=m)
            travel_note = f"{what} в {parsed['deadline']}: лучше прийти за {m} минут ({why}) — к {deadline:%H:%M}."
    if not parsed.get("deadline") and not parsed.get("duration_min"):
        travel_note = (travel_note + " " if travel_note else "") + "Ты не назвал время — взял окно 90 минут. Напиши «к 19:00» или «есть 40 минут», чтобы точнее."
    if parsed.get("deadline") and parsed.get("duration_min"):
        deadline = min(deadline, now + timedelta(minutes=parsed["duration_min"]))
    timings["geocode"] = round((time.time() - t1) * 1000)

    w = geo.weather({"apparent": -25, "wind": 30} if req.weather == "cold" else None)
    intent = {"origin": origin, "destination": destination, "deadline": deadline.isoformat(),
              "prefs": parsed.get("prefs"), "with_kids": parsed.get("with_kids"),
              "mobility": parsed.get("mobility"), "transport": parsed.get("transport"), "group_size": parsed.get("group_size"),
              "language": parsed.get("language")}
    t2 = time.time()
    result = planner.plan(intent, now, w, exclude=req.exclude, required=req.keep)
    timings["plan"] = round((time.time() - t2) * 1000)
    base = {"parsed": parsed, "origin": origin, "destination": destination, "now": now.strftime("%H:%M"),
            "deadline": deadline.strftime("%H:%M"), "window_label": window_label, "weather": w}
    if travel_note:
        result.setdefault("notes", []).insert(0, travel_note)
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
                f"Места: база OpenStreetMap, {len(planner.POIS)} мест Астаны; рассмотрено {result['considered']['candidates']} кандидатов, "
                f"{result['considered']['plans']} вариантов маршрута.",
                "Время пешком: реальный маршрут по улицам (OSRM/FOSSGIS), шаг 4,5 км/ч.",
                "Часы работы: проверены на момент твоего прихода и ухода; нет в данных — «оценка».",
                "Уточнение по 2GIS (Places API): свежие часы работы, рейтинг и отметки вроде «можно с ноутбуком» — "
                "только для мест-кандидатов; помечено «2GIS».",
                f"Погода: {w['source']}.",
                "Тексты «почему» и «что делать»: Gemini 3.5 Flash (Google), только по фактам выше. Места выбирает алгоритм, не ИИ.",
            ]}
