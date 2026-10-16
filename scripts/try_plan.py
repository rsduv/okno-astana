"""Быстрый прогон планировщика из консоли: python scripts/try_plan.py"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime
from app import planner, geo

CASES = {
    "ЭКСПО → Хан Шатыр к 19:00": ("ЭКСПО", "Хан Шатыр", "2026-10-16T17:20", "2026-10-16T19:00", [], None),
    "то же при −25°": ("ЭКСПО", "Хан Шатыр", "2026-10-16T17:20", "2026-10-16T19:00", [], {"apparent": -25, "wind": 30}),
    "Mega, прогулка 45 мин": ("Mega Silk Way", "Mega Silk Way", "2026-10-17T14:00", "2026-10-17T14:45", ["walk", "coffee"], None),
    "Байтерек → аэропорт, 2 ч": ("Байтерек", "аэропорт", "2026-10-17T15:00", "2026-10-17T17:00", ["sights"], None),
    "Ночь у Хан Шатыра 2 ч": ("Хан Шатыр", "Хан Шатыр", "2026-10-17T23:00", "2026-10-18T01:00", ["night"], None),
    "С ребёнком у Керуена 1.5 ч": ("Керуен", "Керуен", "2026-10-17T11:00", "2026-10-17T12:30", ["kids"], None),
}
only = sys.argv[1:] and sys.argv[1]
for name, (o, d, now, dl, prefs, ov) in CASES.items():
    if only and only not in name:
        continue
    it = {"origin": geo.find_place(o), "destination": geo.find_place(d), "deadline": dl, "prefs": prefs}
    t = time.time()
    r = planner.plan(it, datetime.fromisoformat(now), geo.weather(ov))
    print(f"\n== {name}  ({time.time() - t:.1f} c)", r.get("error") or "", r.get("summary"))
    for s in r.get("steps", []):
        if s["type"] == "leg":
            print(f"     {'🚶' if s['mode'] == 'walk' else '🚕'} {s['min']} мин {s['km']} км")
        elif s["type"] == "stop":
            print(f"  * {s['arrive']}-{s['leave']} {s['name']} [{s['category']}] — {s['hours']['text']}")
        else:
            print(f"  {s['type']} {s['time']}")
    for n in r.get("notes", []):
        print("   !", n)
