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

R = llm._rules
check("без ИИ: рейс «at 18:30» → аэропорт к 18:30",
      (lambda d: d["destination"] == "аэропорт" and d["deadline"] == "18:30")(R("I'm at Baiterek, my flight is at 18:30")))
check("без ИИ: «я у Байтерека» — старт, хотя назван вторым",
      (lambda d: d["origin"] == "Байтерек" and d["destination"] == "Назарбаев Университет")(R("мне нужно в Назарбаев Университет к 9, я у Байтерека")))
check("без ИИ: «Назарбаев» — не коляска («арба»)", R("я у Назарбаев Университета, час")["mobility"] == "normal")
check("без ИИ: казахское «бір сағат» = 60 мин, «екі сағат» = 120",
      R("Хан Шатырдың жанындамын, бір сағат бар")["duration_min"] == 60 and R("Байтеректемін, екі сағат бар")["duration_min"] == 120)
check("без ИИ: «19:00-ге» — срок", R("Керуенде тұрмын, 19:00-ге Хан Шатырға баруым керек")["deadline"] == "19:00")

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

# --- API целиком (без ключей: разбор правилами, тексты шаблоном) ---
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
api = TestClient(app)
check("API: главная страница", api.get("/").status_code == 200)
r = api.post("/api/plan", json={"text": "Я в ЭКСПО, к 19:00 надо быть в Хан Шатыре", "now": "17:20"}).json()
check("API: план ЭКСПО → Хан Шатыр", not r.get("error") and r["summary"]["arrive"] <= "19:00")
check("API: у каждой остановки есть «почему», «что делать» и источник",
      all(s.get("why") and s.get("todo") and s.get("source") for s in r.get("steps", []) if s["type"] == "stop"))
check("API: «какие данные использованы» в ответе", len(r.get("data_used") or []) >= 5)
r = api.post("/api/plan", json={"text": "jdjdjd", "now": "15:00"}).json()
check("API: непонятный запрос — честная ошибка, не падение", bool(r.get("error")))
pl = api.get("/api/place", params={"lat": 51.1283, "lon": 71.4305}).json()
check("API: карточка «что здесь» узнаёт Байтерек", (pl.get("poi") or {}).get("name") == "Байтерек")
home = {"Здесь мой дом": {"lat": 51.083, "lon": 71.418, "kind": "home"}}
r = api.post("/api/plan", json={"text": "домой", "now": "14:00", "point": {"lat": 51.157, "lon": 71.437, "mine": True},
                                "saved": home}).json()
check("«домой» = закладка со значком «Дом», как бы она ни называлась",
      not r.get("error") and r.get("destination", {}).get("label") == "Здесь мой дом")
r = api.post("/api/plan", json={"text": "Мне нужно в Назарбаев Университет, потом в Mega Silk Way и в конце домой", "now": "14:00",
                                "point": {"lat": 51.157, "lon": 71.437, "mine": True}, "saved": home,
                                "parsed": {"origin": None, "destination": "домой", "return_to_origin": False, "deadline": None,
                                           "duration_min": None, "prefs": [], "language": "ru",
                                           "via": [{"place": "Назарбаев Университет"}, {"place": "Mega Silk Way"}]}}).json()
st = [s["name"] for s in r.get("steps", []) if s["type"] == "stop"]
check("цепочка «НУ → Мега → домой» в порядке человека", r.get("chain") and len(st) == 2 and "Назарбаев" in st[0] and "Mega" in st[1])
cs = api.get("/api/city").json()
check("API: спрос для города — только счётчики, без координат и текстов",
      cs["requests"] >= 2 and cs["refusals"] >= 1 and "lat" not in str(cs) and "ЭКСПО" not in str(cs))

print(f"\n{ok} ок, {fail} провалено")
sys.exit(1 if fail else 0)
