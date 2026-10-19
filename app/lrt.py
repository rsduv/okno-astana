"""LRT Астаны (линия Әуежай — Нұрлы Жол, открыта 16.05.2026): подсказка «можно доехать на LRT» для длинных отрезков на такси.

Станции и их порядок — из OpenStreetMap (scripts/collect_city.py → data/lrt.json). Расписания в открытых данных нет,
поэтому время — оценка: средняя скорость поезда, ~30 с на каждой станции и ожидание половины интервала.
"""
import json
from pathlib import Path

from . import geo

ROOT = Path(__file__).resolve().parent.parent
_f = ROOT / "data" / "lrt.json"
STATIONS = json.loads(_f.read_text(encoding="utf-8"))["stations"] if _f.exists() else []

SPEED_KMH = 35        # средняя скорость с разгоном и торможением (оценка)
DWELL_MIN = 0.5       # стоянка на станции
WAIT_MIN = 5          # половина интервала ~10 мин (оценка, расписания в открытых данных нет)
MAX_WALK_KM = 1.1     # дальше до станции — уже не «на LRT»


def _nearest(pt):
    if not STATIONS:
        return None, 99.0
    i = min(range(len(STATIONS)), key=lambda k: geo.haversine_km(pt, (STATIONS[k]["lat"], STATIONS[k]["lon"])))
    return i, geo.haversine_km(pt, (STATIONS[i]["lat"], STATIONS[i]["lon"]))


def trip(a, b):
    """(станция А, станция Б, минут всего с дорогой до станций) или None, если LRT тут не помогает."""
    ia, da = _nearest(a)
    ib, db = _nearest(b)
    if ia is None or abs(ia - ib) < 3 or da > MAX_WALK_KM or db > MAX_WALK_KM:   # 1–2 станции — проще такси или пешком
        return None
    lo, hi = sorted((ia, ib))
    km = sum(geo.haversine_km((STATIONS[k]["lat"], STATIONS[k]["lon"]), (STATIONS[k + 1]["lat"], STATIONS[k + 1]["lon"]))
             for k in range(lo, hi)) * 1.1
    ride = km / SPEED_KMH * 60 + (hi - lo) * DWELL_MIN
    walk = geo.walk_min_est(a, (STATIONS[ia]["lat"], STATIONS[ia]["lon"])) + \
        geo.walk_min_est((STATIONS[ib]["lat"], STATIONS[ib]["lon"]), b)
    return STATIONS[ia]["name"], STATIONS[ib]["name"], round(walk + WAIT_MIN + ride), hi - lo


def advice(steps: list, end: str = "финиш", buffer_min: int = 0) -> str | None:
    """Для отрезков на такси от 12 мин: если вдоль пути есть LRT и он не сильно дольше — подсказка с оценкой."""
    names = [s.get("name") for s in steps if s["type"] == "stop"] + [end]
    out, i = [], 0
    for s in steps:
        if s["type"] == "stop":
            i += 1
        if s["type"] != "leg" or s.get("mode") != "taxi" or (s.get("min") or 0) < 12 or not s.get("coords"):
            continue
        c = s["coords"]
        t = trip((c[0][1], c[0][0]), (c[-1][1], c[-1][0]))
        if t and t[2] <= s["min"] + min(30, max(0, buffer_min)):   # дольше такси — только если запас это позволяет
            to = names[i] if i < len(names) else end
            cmp = "почти так же, как такси" if t[2] <= s["min"] + 8 else f"дольше такси (~{s['min']} мин), но дешевле и без пробок"
            out.append(f"до «{to}» можно на LRT: «{t[0]}» → «{t[1]}», {t[3]} ст., ~{t[2]} мин вместе с дорогой до станций — {cmp}")
    if not out:
        return None
    return ("Вместо такси: " + "; ".join(out) +
            ". Время LRT — оценка: расписания в открытых данных нет (линия открыта 16.05.2026).")
