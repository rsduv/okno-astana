"""Быстрые тесты ядра без сети и ключей: python scripts/test_core.py"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["DGIS_OFF"] = "1"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import geo, llm, planner  # noqa: E402
from app.hours import interval_status  # noqa: E402
from app.main import OTHER_CITIES, explicit_cats  # noqa: E402

D = datetime(2026, 10, 18)  # воскресенье
ok = fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("FAIL:", name)


def at(h, m=0, day=0):
    return D.replace(day=D.day + day, hour=h, minute=m)


# --- часы работы ---
st = lambda oh, a, b, cat="кафе": interval_status({"opening_hours": oh} if oh else {}, cat, a, b)[0]
check("обычные часы — открыто", st("Mo-Su 10:00-22:00", at(12), at(13)) == "open")
check("закроется во время визита", st("Mo-Su 10:00-22:00", at(21, 40), at(22, 10)) == "closes_soon")
check("до открытия — закрыто", st("Mo-Su 10:00-22:00", at(8), at(9)) == "closed")
check("через полночь (до 02:00)", st("Mo-Su 18:00-02:00", at(23, 30), at(0, 30, 1)) == "open")
check("24/7", st("24/7", at(3), at(4)) == "open")
check("00:00-23:59 каждый день = круглосуточно",
      st("; ".join(f"{d} 00:00-23:59" for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]), at(23, 50), at(0, 20, 1)) == "open")
check("выходной в воскресенье", st("Mo-Sa 10:00-18:00; Su off", at(12), at(13)) == "closed")
check("уличное место без часов — открыто", interval_status({}, "парк", at(12), at(13))[1] == "outdoor")
check("нет часов — оценка", interval_status({}, "кафе", at(12), at(13))[1] == "estimate")

# --- поиск мест ---
S = {"Тётя Гуля": {"lat": 51.16, "lon": 71.41}, "Дом": {"lat": 51.1, "lon": 71.4}}
check("своё место в другом падеже", (geo.find_place("к тёте Гуле", None, S) or {}).get("label") == "Тётя Гуля")
check("«домой» = Дом", (geo.find_place("домой", None, S) or {}).get("label") == "Дом")
check("ориентир по сокращению (мну)", "Narikbayev" in (geo.find_place("мну") or {}).get("label", ""))
check("ориентир Барыс Арена", (geo.find_place("Барыс Арена") or {}).get("label") == "Барыс Арена")

# --- понимание запроса без ИИ ---
check("явный тип: музей", explicit_cats("хочу в музей") == ["музей"])
check("«Барыс» — не бар", explicit_cats("концерт в Барыс Арене") == [])
check("«в баре» — бар", explicit_cats("посидеть в баре") == ["бар"])
check("казахское «бар» = «есть», не бар", "бар" not in explicit_cats("бір сағат бар, кофе ішкім келеді"))
check("парк", "парк" in explicit_cats("погулять в парке"))
check("другой город", bool(OTHER_CITIES.match("Москве")) and not OTHER_CITIES.match("БЦ Москва"))
check("«полтора часа» без ИИ", llm._rules("с ребёнком у Керуена, полтора часа")["duration_min"] == 90)
check("казахский без ИИ", llm._rules("Хан Шатырдың жанындамын, бір сағат бар")["language"] == "kk")

# --- планировщик: инварианты ---
o = geo.find_place("ЭКСПО")
d = geo.find_place("Хан Шатыр")
r = planner.plan({"origin": o, "destination": d, "deadline": "2026-10-18T19:00", "prefs": []}, at(17, 20), geo.weather({"apparent": -25, "wind": 30}))
stops = [s for s in r.get("steps", []) if s["type"] == "stop"]
check("ЭКСПО→Хан Шатыр: есть план", not r.get("error") and 1 <= len(stops) <= 3)
check("успевает к сроку", r.get("summary", {}).get("arrive", "99") <= "19:00")
check("все места открыты весь визит", all(s["hours"]["status"] == "open" for s in stops))
check("в мороз нет уличных мест", all(s["hours"]["confidence"] != "outdoor" for s in stops))
check("в мороз ≤ 12 мин на улице подряд", r.get("summary", {}).get("max_outdoor_leg", 99) <= 12)
lim = planner.plan({"origin": o, "destination": {**o, "label": "обратно"}, "deadline": "2026-10-18T13:00", "prefs": [],
                    "mobility": "limited"}, at(12), geo.weather())
check("коляска: заметка о медленном темпе", any("спокойного темпа" in n for n in lim.get("notes", [])))

# --- афиша: событие предлагаем, только если оно целиком помещается в окно ---
from app import events  # noqa: E402
events.EVENTS[:] = [{"id": "t", "title": "Тест-концерт", "date": "2026-10-18", "time": "15:00", "minutes": 60, "hall": "Камерный зал",
                     "kind": "Концерт", "age": "5", "url": "https://example.org", "venue": "Астана Опера", "lat": 51.13546, "lon": 71.410659}]
bt = (51.128, 71.4305)
check("афиша: концерт помещается в окно 13:30–16:30", len(events.fitting(bt, bt, at(13, 30), at(16, 30))) == 1)
check("афиша: не успеть к началу — не предлагаем", events.fitting(bt, bt, at(14, 58), at(18)) == [])
check("афиша: закончится после срока — не предлагаем", events.fitting(bt, bt, at(13, 30), at(15, 50)) == [])
check("афиша: спросил про театр — ближайшие события", len(events.upcoming(bt, at(9))) == 1 and events.WANTS.search("хочу в театр"))

print(f"\n{ok} ок, {fail} провалено")
sys.exit(1 if fail else 0)
