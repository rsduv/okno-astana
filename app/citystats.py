"""Обезличенная статистика спроса для города (B2G): что ищут люди и чего им не хватает.

Только счётчики в памяти процесса: ни координат, ни текстов запросов, ни времени конкретного человека.
После перезапуска сервера всё обнуляется — это демонстрация того, какую пользу «Окно» может дать акимату.
"""
from __future__ import annotations

import re
import threading
from collections import Counter
from datetime import datetime

_lock = threading.Lock()
STARTED = datetime.now()
S = {"requests": 0, "plans": 0, "refusals": 0, "cold": 0, "events_shown": 0,
     "prefs": Counter(), "hours": Counter(), "langs": Counter(), "categories": Counter(), "gaps": Counter(), "people": Counter()}

GAP = [  # заметки планировщика, которые говорят «в городе этого не хватило» → понятная городу формулировка
    (re.compile(r"Просил «([^»]+)», но в это время"), "«{}» — закрыто в нужное время"),
    (re.compile(r"Просил «([^»]+)», но по времени"), "«{}» — далеко, не успеть"),
    (re.compile(r"Необычного места"), "необычные места (квесты, арт) — нет рядом открытых"),
    (re.compile(r"казахская кухня"), "национальная кухня — нет по пути"),
    (re.compile(r"Лучший вид на город"), "смотровые площадки — далеко"),
    (re.compile(r"часов работы нет в данных"), "у мест нет часов работы в открытых данных"),
]


def record(parsed: dict, result: dict, cold: bool, n_events: int, hour: int) -> None:
    with _lock:
        S["requests"] += 1
        S["hours"][f"{hour:02d}:00"] += 1
        S["langs"][parsed.get("language") or "ru"] += 1
        for p in parsed.get("prefs") or []:
            S["prefs"][p] += 1
        if parsed.get("with_kids"):
            S["people"]["с детьми"] += 1
        if parsed.get("mobility") == "limited":
            S["people"]["коляска / трудно ходить"] += 1
        if (parsed.get("group_size") or 0) >= 3:
            S["people"]["компания 3+"] += 1
        if cold:
            S["cold"] += 1
        S["events_shown"] += 1 if n_events else 0
        if result.get("error"):
            S["refusals"] += 1
        else:
            S["plans"] += 1
            for st in result.get("steps") or []:
                if st.get("type") == "stop":
                    S["categories"][st.get("category")] += 1
        for n in (result.get("notes_original") or result.get("notes") or []):
            for rx, label in GAP:
                m = rx.search(n)
                if m:
                    S["gaps"][label.format(*m.groups())] += 1


def refused(lang: str | None) -> None:
    with _lock:
        S["requests"] += 1
        S["refusals"] += 1
        S["langs"][lang or "ru"] += 1
        S["gaps"]["не понял, где человек, или место вне Астаны"] += 1


def snapshot() -> dict:
    with _lock:
        top = lambda c, k=8: [[a, b] for a, b in c.most_common(k)]
        return {"since": STARTED.strftime("%d.%m %H:%M"), "requests": S["requests"], "plans": S["plans"], "refusals": S["refusals"],
                "cold": S["cold"], "events_shown": S["events_shown"], "prefs": top(S["prefs"]), "hours": sorted(S["hours"].items()),
                "langs": top(S["langs"]), "categories": top(S["categories"]), "gaps": top(S["gaps"]), "people": top(S["people"])}
