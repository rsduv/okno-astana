"""Афиша: события, которые помещаются в окно человека. Данные: официальный сайт Astana Opera (scripts/collect_events.py →
data/events.json) и городская афиша sxodim.com — выставки, концерты, вечеринки, квизы, детское (scripts/collect_sxodim.py →
data/events_city.json).

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
CITY = Path(__file__).resolve().parent.parent / "data" / "events_city.json"


def _load(f: Path) -> dict:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"events": []}


_D, _C = _load(FILE), _load(CITY)
EVENTS: list[dict] = _D.get("events", []) + _C.get("events", [])
UPDATED: str | None = _D.get("updated")
UPDATED_CITY: str | None = _C.get("updated")
WANTS = re.compile(r"опер|балет|концерт|спектак|театр|афиш|музык|джаз|jazz|concert|ballet|opera|theat|show|шоу|қойылым|концерт|"
                   r"выстав|квиз|quiz|вечерин|party|тусов|стендап|stand.?up|комик|фестив|festival|exhibit|лекци|мастер.?класс|"
                   r"матч|хокке|барыс|событи|мероприят|event|что.{0,12}интересн|көрме|кеш", re.I)
# что в запросе → какие события подходят по смыслу
TOPICS = [(r"выстав|exhibit|музе|искусств|art|көрме", r"выстав"), (r"квиз|quiz|викторин|игр", r"игр|квиз"),
          (r"вечерин|party|тусов|танц|клуб|dance", r"вечерин"), (r"стендап|stand.?up|комик|юмор|посмеят", r"стендап|stand"),
          (r"концерт|музык|джаз|jazz|concert|live", r"концерт"), (r"театр|спектак|theat|мюзикл|қойылым", r"театр|спектак|мюзикл"),
          (r"матч|хокке|барыс|футбол|спорт|match|hockey", r"матч|спорт|барыс"), (r"кино|фильм|movie|cinema", r"кино"),
          (r"опер|балет|ballet|opera", r"опер|балет|концерт")]


def relevance(e: dict, text: str, prefs: set, kids: bool) -> float:
    """Насколько событие подходит запросу. 0 — не по теме (тогда городские события не навязываем)."""
    t, k = (text or "").lower(), f"{e.get('kind', '')} {e['title']}".lower()
    s = sum(3.0 for q, ev in TOPICS if re.search(q, t) and re.search(ev, k))
    child = bool(re.search(r"дет|kids|ребён|ребен|малыш|бала", k))
    if kids and child:
        s += 3.0
    if child and not kids:
        return 0.0          # детский утренник взрослому не предлагаем
    if prefs & {"unusual", "culture", "night"} or WANTS.search(t):
        s += 1.0
    if e.get("source") == "astanaopera.kz":
        s += 0.5            # официальная площадка, знаковое место города
    return s


def _travel(a, b, t) -> tuple[float, str]:
    walk = geo.walk_min_est(a, b)
    car = geo.car_min_est(a, b, t) + geo.TAXI_WAIT_MIN
    return (walk, "walk") if walk <= 20 or walk <= car else (car, "taxi")


def _card(e: dict, start: datetime, go: float, mode: str) -> dict:
    return {"title": e["title"], "date": start.strftime("%Y-%m-%d"), "time": start.strftime("%H:%M"), "minutes": e["minutes"],
            "end": (start + timedelta(minutes=e["minutes"])).strftime("%H:%M"), "hall": e.get("hall"), "kind": e.get("kind"),
            "age": e.get("age"), "venue": e["venue"], "url": e["url"], "lat": e["lat"], "lon": e["lon"],
            "go_min": round(go), "mode": mode, "source": e.get("source"), "price": e.get("price"),
            "range": bool(e.get("range")), "until": e.get("until")}


def _start(e: dict, now: datetime, go: float) -> datetime | None:
    """Время начала: у сеанса — по афише; у выставки на период — когда дойдёшь (если она сегодня идёт и открыта)."""
    if not e.get("range"):
        return datetime.fromisoformat(f"{e['date']}T{e['time']}")
    if not re.search(r"выстав|exhibit|көрме", f"{e.get('kind', '')} {e['title']}", re.I):
        return None    # шоу и киноужины «на период» без времени начала — в окно не ставим, не зная сеанса
    day = now.strftime("%Y-%m-%d")
    if not (e["date"] <= day <= e.get("until", e["date"])):
        return None
    t = now + timedelta(minutes=go + 10)
    open_, close = t.replace(hour=10, minute=0), t.replace(hour=20, minute=0)   # обычные часы выставок; точных в данных нет
    return t if open_ <= t and t + timedelta(minutes=e["minutes"]) <= close else None


def fitting(A, B, now: datetime, deadline: datetime, text: str = "", prefs: set = frozenset(), kids: bool = False) -> list[dict]:
    """События сегодня, которые целиком помещаются в окно (доехать к началу → посмотреть → успеть к сроку).
    Городские события (не официальная площадка) показываем, только если они по теме запроса."""
    out = []
    for e in EVENTS:
        if not e.get("range") and e["date"] != now.strftime("%Y-%m-%d"):
            continue
        rel = relevance(e, text, set(prefs), kids)
        if rel <= 0 or (e.get("source") != "astanaopera.kz" and rel < 1):
            continue
        venue = (e["lat"], e["lon"])
        go, mode = _travel(A, venue, now)
        t0 = _start(e, now, go)
        if t0 is None:
            continue
        t1 = t0 + timedelta(minutes=e["minutes"])
        back, _ = _travel(venue, B, t1)
        if t0 - timedelta(minutes=10) >= now + timedelta(minutes=go) - timedelta(seconds=1) and t1 + timedelta(minutes=back) <= deadline:
            out.append((rel, t0, _card(e, t0, go, mode)))
    out.sort(key=lambda x: (-min(x[0], 3), x[1]))   # по теме — да; среди подходящих — что раньше
    return [c for _, _, c in out[:2]]


def upcoming(A, now: datetime, n: int = 3, text: str = "", prefs: set = frozenset(), kids: bool = False) -> list[dict]:
    """Если человек сам спрашивает про события, а в его окно ничего не помещается — ближайшие подходящие по теме."""
    cand = []
    for e in EVENTS:
        if e.get("range"):
            continue    # выставки на период уже идут — их показывает fitting, если успеваешь
        t0 = datetime.fromisoformat(f"{e['date']}T{e['time']}")
        rel = relevance(e, text, set(prefs), kids)
        if t0 < now + timedelta(minutes=30) or rel <= 0:
            continue
        cand.append((rel, t0, e))
    cand.sort(key=lambda x: (-min(x[0], 3), x[1]))   # сегодняшний концерт важнее такого же через неделю
    out = []
    for _, t0, e in cand[:n]:
        go, mode = _travel(A, (e["lat"], e["lon"]), now)
        out.append({**_card(e, t0, go, mode), "later": True})
    return sorted(out, key=lambda c: (c["date"], c["time"]))
