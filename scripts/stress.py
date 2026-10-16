"""Стресс-тест планировщика: случайные запросы, проверка инвариантов. python scripts/stress.py [N]"""
import os, random, sys, time, traceback
os.environ["DGIS_OFF"] = "1"  # не тратим лимит демо-ключа 2GIS на стресс-тест
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime, timedelta
from app import planner, geo
from app.hours import interval_status

random.seed(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
LM = geo.LANDMARKS
PREFS = ["coffee", "food", "sights", "nature", "culture", "kids", "night", "work", "walk"]
ok = err = empty = 0
bad = []
t0 = time.time()
for k in range(N):
    a = random.choice(LM); b = random.choice(LM + [a, a])
    now = datetime(2026, 10, 17, random.randint(0, 23), random.choice([0, 20, 40]))
    window = random.choice([20, 30, 45, 60, 90, 120, 180])
    prefs = random.sample(PREFS, random.choice([0, 0, 1, 2]))
    w = geo.weather({"apparent": -25, "wind": 30}) if random.random() < 0.3 else geo.weather()
    it = {"origin": {"lat": a["lat"], "lon": a["lon"], "label": a["name"]},
          "destination": {"lat": b["lat"], "lon": b["lon"], "label": b["name"]},
          "deadline": (now + timedelta(minutes=window)).isoformat(), "prefs": prefs}
    try:
        r = planner.plan(it, now, w)
    except Exception:
        err += 1; bad.append((a["name"], b["name"], now, window, prefs, traceback.format_exc()[-300:])); continue
    if r.get("error"):
        empty += 1; continue
    # инварианты: успеваем к сроку, каждое место открыто всё время визита
    end = [s for s in r["steps"] if s["type"] == "end"][0]["time"]
    fin = now.replace(hour=int(end[:2]), minute=int(end[3:]))
    if fin < now: fin += timedelta(days=1)
    if fin > now + timedelta(minutes=window):
        bad.append(("ОПОЗДАНИЕ", a["name"], b["name"], now, window, end))
    for s in r["steps"]:
        if s["type"] == "stop" and s["hours"]["status"] != "open":
            bad.append(("ЗАКРЫТО", s["name"], s["hours"]))
    ok += 1
print(f"{N} запросов за {time.time()-t0:.0f} c: план {ok}, честный отказ {empty}, ошибок {err}, нарушений {len(bad) - err}")
for x in bad[:10]:
    print("  ", x)
