"""Сервер «Окна». Запуск: python -m uvicorn app.main:app --port 8000  →  http://localhost:8000"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
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


@app.post("/api/plan")
def make_plan(req: PlanReq):
    timings = {}
    t0 = time.time()
    now = _now(req.now)
    parsed = req.parsed or llm.understand(req.text, now)
    timings["understand"] = round((time.time() - t0) * 1000)

    t1 = time.time()
    if req.point and req.point.get("lat") is not None:
        lat, lon = geo.round_point(req.point["lat"], req.point["lon"])
        origin = {"lat": lat, "lon": lon, "label": "Точка на карте (±150 м)", "source": "карта, округлено"}
    else:
        origin = geo.find_place(parsed.get("origin") or "", planner.POIS)
    if not origin:
        return JSONResponse({"error": "Не понял, где ты сейчас. Напиши ориентир («я у Байтерека») или ткни в карту.",
                             "parsed": parsed}, status_code=200)
    dest = None
    if parsed.get("destination") and not parsed.get("return_to_origin"):
        dest = geo.find_place(parsed["destination"], planner.POIS)
        if not dest:
            return {"error": f"Не нашёл «{parsed['destination']}» в Астане. Попробуй назвать иначе.", "parsed": parsed}
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
    if not parsed.get("deadline") and not parsed.get("duration_min"):
        travel_note = (travel_note + " " if travel_note else "") + "Ты не назвал время — взял окно 90 минут. Напиши «к 19:00» или «есть 40 минут», чтобы точнее."
    if parsed.get("deadline") and parsed.get("duration_min"):
        deadline = min(deadline, now + timedelta(minutes=parsed["duration_min"]))
    timings["geocode"] = round((time.time() - t1) * 1000)

    w = geo.weather({"apparent": -25, "wind": 30} if req.weather == "cold" else None)
    intent = {"origin": origin, "destination": destination, "deadline": deadline.isoformat(),
              "prefs": parsed.get("prefs"), "with_kids": parsed.get("with_kids"),
              "mobility": parsed.get("mobility"), "transport": parsed.get("transport"),
              "language": parsed.get("language")}
    t2 = time.time()
    result = planner.plan(intent, now, w, exclude=req.exclude, required=req.keep)
    timings["plan"] = round((time.time() - t2) * 1000)
    base = {"parsed": parsed, "origin": origin, "destination": destination, "now": now.strftime("%H:%M"),
            "deadline": deadline.strftime("%H:%M"), "window_label": window_label, "weather": w}
    if travel_note:
        result.setdefault("notes", []).insert(0, travel_note)
    if result.get("error"):
        return {**base, **result, "timings": timings}
    t3 = time.time()
    result = llm.explain(req.text, intent, result, w)
    timings["explain"] = round((time.time() - t3) * 1000)
    return {**base, **result, "timings": timings,
            "data_used": [
                f"Твоя точка: {origin['label']} — {origin['source']}; координаты не сохраняются.",
                f"Места: база OpenStreetMap, {len(planner.POIS)} мест Астаны; рассмотрено {result['considered']['candidates']} кандидатов, "
                f"{result['considered']['plans']} вариантов маршрута.",
                "Время пешком: реальный маршрут по улицам (OSRM/FOSSGIS), шаг 4,5 км/ч.",
                "Часы работы: проверены на момент твоего прихода и ухода; нет в данных — «оценка».",
                f"Погода: {w['source']}.",
                "Тексты «почему» и «что делать»: Gemini, только по фактам выше. Места выбирает алгоритм, не ИИ.",
            ]}
