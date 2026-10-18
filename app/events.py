"""Афиша: события, которые помещаются в окно человека (данные — scripts/collect_events.py → data/events.json).

Событие предлагаем, только если человек успевает доехать к началу (с запасом 10 минут на вход) и после окончания
добраться к своему сроку. Это реальные данные с официального сайта площадки, а не выдумка ИИ.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import geo

FILE = Path(__file__).resolve().parent.parent / "data" / "events.json"
try:
    _D = json.loads(FILE.read_text(encoding="utf-8"))
except Exception:
    _D = {"events": []}
EVENTS: list[dict] = _D.get("events", [])
UPDATED: str | None = _D.get("updated")
WANTS = re.compile(r"опер|балет|концерт|спектак|театр|афиш|музык|джаз|jazz|concert|ballet|opera|theat|show|шоу|қойылым|концерт", re.I)


def _travel(a, b, t) -> tuple[float, str]:
    walk = geo.walk_min_est(a, b)
    car = geo.car_min_est(a, b, t) + geo.TAXI_WAIT_MIN
    return (walk, "walk") if walk <= 20 or walk <= car else (car, "taxi")


def _card(e: dict, start: datetime, go: float, mode: str) -> dict:
    return {"title": e["title"], "date": e["date"], "time": e["time"], "minutes": e["minutes"],
            "end": (start + timedelta(minutes=e["minutes"])).strftime("%H:%M"), "hall": e.get("hall"), "kind": e.get("kind"),
            "age": e.get("age"), "venue": e["venue"], "url": e["url"], "lat": e["lat"], "lon": e["lon"],
            "go_min": round(go), "mode": mode, "source": e.get("source")}


def fitting(A, B, now: datetime, deadline: datetime) -> list[dict]:
    """События сегодня, которые целиком помещаются в окно (доехать к началу → посмотреть → успеть к сроку)."""
    out = []
    for e in EVENTS:
        if e["date"] != now.strftime("%Y-%m-%d"):
            continue
        t0 = datetime.fromisoformat(f"{e['date']}T{e['time']}")
        t1 = t0 + timedelta(minutes=e["minutes"])
        venue = (e["lat"], e["lon"])
        go, mode = _travel(A, venue, now)
        back, _ = _travel(venue, B, t1)
        if t0 - timedelta(minutes=10) >= now + timedelta(minutes=go) and t1 + timedelta(minutes=back) <= deadline:
            out.append(_card(e, t0, go, mode))
    return out[:2]


def upcoming(A, now: datetime, n: int = 3) -> list[dict]:
    """Если человек сам спрашивает про театр/концерт, а в его окно ничего не помещается — ближайшие события."""
    out = []
    for e in EVENTS:
        t0 = datetime.fromisoformat(f"{e['date']}T{e['time']}")
        if t0 < now + timedelta(minutes=30):
            continue
        go, mode = _travel(A, (e["lat"], e["lon"]), now)
        out.append({**_card(e, t0, go, mode), "later": True})
        if len(out) >= n:
            break
    return out
