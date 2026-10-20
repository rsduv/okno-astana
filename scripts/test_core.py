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
                     "kind": "Концерт", "age": "5", "url": "https://example.org", "venue": "Астана Опера", "lat": 51.13546, "lon": 71.410659, "source": "astanaopera.kz"}]
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
from app.main import start_offset
from app import geo as _g
check("«через полчаса выйду» = старт через 30 мин", start_offset("Через полчаса выйду из дома") == 30
      and start_offset("выезжаю через 20 минут") == 20 and start_offset("через час в кино") == 0)
check("падежи ориентиров: «в Мегу» = Mega Silk Way, «Мега Астану» = MEGA Astana",
      _g.find_place("Мегу")["label"] == "Mega Silk Way" and _g.find_place("Мега Астану")["label"] == "MEGA Astana")
check("ориентиры по порядку: «в АИТУ, потом в Мегу, потом к Байтереку»",
      _g.landmarks_in_order("поеду в АИТУ, потом в Мегу, потом к Байтереку") == ["Astana IT University (АИТУ)", "Mega Silk Way", "Байтерек"])
check("АИТУ — свой ориентир, не центр шара EXPO", "АИТУ" in _g.find_place("АИТУ")["label"])
# сценарии передвижения (живые фразы с ИИ — scripts/trips.py; здесь — с готовым разбором, без сети ИИ)
def trip(text, now, via, dest=None, back=False, saved=None, origin=None, deadline=None):
    return api.post("/api/plan", json={"text": text, "now": now, "saved": saved or {}, "parsed": {
        "origin": origin, "destination": dest, "return_to_origin": back, "deadline": deadline, "duration_min": None,
        "prefs": [], "language": "ru", "via": [{"place": v, "stay_min": None} for v in via]}}).json()


r = trip("Я дома на Мангилик Ел 53. Потом в АИТУ к 9:00, после пар в Мегу пообедать, потом домой", "08:20",
         ["АИТУ", "Мега"], dest="домой", back=True, origin="Мангилик Ел 53", deadline="09:00")
st = {s["name"]: s for s in r.get("steps", []) if s["type"] == "stop"}
mega = next((v for k, v in st.items() if "Mega" in k), None)
check("поездка: «в АИТУ к 9:00» — срок к АИТУ, а не ко всему маршруту; Мега ждёт открытия 10:00",
      not r.get("error") and mega and mega["arrive"] >= "10:00" and any("к 09:00 успеваешь" in n for n in r.get("notes", []))
      and any("открывается в 10:00" in n for n in r.get("notes", [])))
r = trip("Через полчаса выйду из дома, в Хан Шатыр, потом домой", "12:00", ["Хан Шатыр"], dest="домой",
         saved={"Квартира": {"lat": 51.1605, "lon": 71.4704, "kind": "home"}}, origin="дом")
check("поездка: «через полчаса выйду из дома» — старт из закладки «Дом» в 12:30",
      not r.get("error") and r["origin"]["label"] == "Квартира" and r["steps"][0]["time"] == "12:30"
      and r["destination"]["label"] == "Квартира")
r = trip("Я у Байтерека, потом в Хан Шатыр, потом в Ак Орду, потом обратно. Такси или пешком?", "11:00",
         ["Хан Шатыр", "Ак Орда"], back=True, origin="Байтерек")
check("поездка: «такси или пешком?» — ответ по каждому отрезку",
      not r.get("error") and any(n.startswith("Такси или пешком") for n in r.get("notes", [])))
r = trip("Я на вокзале Нурлы Жол, потом в Мега Силк Вэй, потом к Байтереку, потом в аэропорт", "14:00",
         ["Мега Силк Вэй", "Байтерек"], dest="аэропорт", origin="вокзал Нурлы Жол")
names = [s["name"] for s in r.get("steps", []) if s["type"] == "stop"]
check("поездка: вокзал → Мега → Байтерек → аэропорт, в порядке человека",
      not r.get("error") and len(names) == 2 and "Mega" in names[0] and "Байтерек" in names[1] and "Аэропорт" in r["destination"]["label"])
# LRT: станции из OSM, подсказка для длинного отрезка на такси
from app import lrt  # noqa: E402
A, B = geo.find_place("Байтерек"), geo.find_place("аэропорт")
t = lrt.trip((A["lat"], A["lon"]), (B["lat"], B["lon"]))
check("LRT: 18 станций, Байтерек → Аэропорт по линии, время — оценка",
      len(lrt.STATIONS) == 18 and t and t[0] == "Байтерек" and t[1] == "Аэропорт" and 30 <= t[2] <= 90)
A, B = geo.find_place("Хан Шатыр"), geo.find_place("Керуен")
check("LRT: рядом с линией нет станций — подсказки нет", lrt.trip((A["lat"], A["lon"]), (B["lat"], B["lon"])) is None)
# найдено оценкой качества (scripts/evaluate.py) и исправлено
check("«рядом» про место в 18 мин пешком заменяется на честное время",
      llm._near_fix("Уютное кафе рядом — отдохнуть", "пешком 18 мин", "ru") == "Уютное кафе в 18 мин пешком — отдохнуть"
      and llm._near_fix("Кафе рядом", "пешком 6 мин", "ru") == "Кафе рядом")
r = api.post("/api/plan", json={"text": "Бабушке 75 лет, тяжело ходить, мы у ЭКСПО. Куда сходить ненадолго?", "now": "13:00",
                                "saved": {}, "parsed": {"origin": "ЭКСПО", "destination": None, "return_to_origin": True,
                                                        "deadline": None, "duration_min": 90, "prefs": [], "language": "ru",
                                                        "via": [], "mobility": "limited"}}).json()
check("маломобильным — пешком не больше 10 мин подряд",
      all(s["min"] <= 12 for s in r.get("steps", []) if s["type"] == "leg" and s["mode"] == "walk"))
r = api.post("/api/plan", json={"text": "Мы компанией у Хан Шатыра, куда сходить вечером?", "now": "13:00", "saved": {}, "parsed": {
    "origin": "Хан Шатыр", "destination": None, "return_to_origin": True, "deadline": None, "duration_min": 120, "prefs": [],
    "language": "ru", "via": [], "group_size": 4}}).json()
check("«куда вечером?» днём — план с 18:00", not r.get("error") and r["steps"][0]["time"] == "18:00")
r = api.post("/api/photo", json={"image": "not an image"}).json()
check("фото: не картинка — понятная ошибка, без падения", bool(r.get("error")))
# городская афиша: событие по теме запроса, детское — только с ребёнком, выставка — если успеваешь до закрытия
from app import events  # noqa: E402
from datetime import datetime as _dt  # noqa: E402
_keep = events.EVENTS
_A = (51.1283, 71.4305)
events.EVENTS = [
    {"title": "Квиз, плиз! #1", "kind": "Игры (квизы, мафия и другое)", "date": "2026-10-20", "time": "20:00", "minutes": 120,
     "venue": "Бар", "url": "u1", "lat": 51.1290, "lon": 71.4310, "source": "sxodim.com"},
    {"title": "Новогодний утренник", "kind": "Для детей", "date": "2026-10-20", "time": "19:00", "minutes": 60,
     "venue": "ДК", "url": "u2", "lat": 51.1285, "lon": 71.4300, "source": "sxodim.com"},
    {"title": "Выставка «Айвазовский»", "kind": "Выставки и ярмарки", "date": "2026-10-20", "until": "2026-10-25", "time": None,
     "range": True, "minutes": 60, "venue": "Музей", "url": "u3", "lat": 51.1288, "lon": 71.4302, "source": "sxodim.com"}]
r1 = events.fitting(_A, _A, _dt(2026, 10, 20, 19, 30), _dt(2026, 10, 20, 23, 0), text="хочу на квиз вечером")
r2 = events.fitting(_A, _A, _dt(2026, 10, 20, 18, 30), _dt(2026, 10, 20, 21, 0), text="куда сходить, что-нибудь интересное")
r3 = events.fitting(_A, _A, _dt(2026, 10, 20, 14, 0), _dt(2026, 10, 20, 16, 0), text="хочу на выставку")
r4 = events.fitting(_A, _A, _dt(2026, 10, 20, 19, 30), _dt(2026, 10, 20, 22, 0), text="хочу на выставку")
events.EVENTS = _keep
check("афиша: «квиз» → квиз", [e["title"] for e in r1][:1] == ["Квиз, плиз! #1"])
check("афиша: детский утренник взрослому не предлагаем", all("утренник" not in e["title"] for e in r2))
check("афиша: выставка днём — да, в 19:30 (закрыта) — нет",
      any("Айвазовский" in e["title"] for e in r3) and not any("Айвазовский" in e["title"] for e in r4))
check("рейтинг в тексте — только настоящий из 2GIS",
      llm._rating_fix("Парк с рейтингом 5 в 2GIS, для детей", {}) == "Парк, для детей"
      and "4,7 в 2GIS" in llm._rating_fix("Кафе с рейтингом 4.9 в 2GIS", {"рейтинг_2гис": "4.7 (120 оценок)"}))
# «встретиться посередине»: место, куда обоим добираться примерно одинаково, открыто к встрече
r = api.post("/api/plan", json={"text": "Я у Хан Шатыра, друг в ЭКСПО, где встретиться выпить кофе?", "now": "15:00", "saved": {},
                                "parsed": {"origin": "Хан Шатыр", "destination": "ЭКСПО", "return_to_origin": False, "deadline": None,
                                           "duration_min": None, "prefs": ["coffee"], "language": "ru", "via": []}}).json()
mt = r.get("meet") or {}
check("встреча посередине: путь друга есть, разница во времени дороги небольшая, место открыто",
      not r.get("error") and r.get("friend") and abs(mt.get("you_min", 0) - mt.get("friend_min", 99)) <= 10
      and all(s["hours"]["status"] == "open" for s in r["steps"] if s["type"] == "stop"))
r = trip("Мы с другом у Абу Даби Плазы. Сначала в Мега Силк Вэй поесть, потом в АИТУ на встречу к 16:00", "13:30",
         ["Мега Силк Вэй"], dest="АИТУ", origin="Абу Даби Плаза", deadline="16:00")
check("«идём с другом… на встречу к 16:00» — обычный маршрут, не «встретиться посередине»", not r.get("meet") and not r.get("error"))
r = api.post("/api/plan", json={"text": "Я у Хан Шатыра, друг в ЭКСПО, где встретиться выпить кофе?", "now": "15:00", "saved": {},
                                "parsed": {"origin": "Хан Шатыр", "destination": None, "return_to_origin": False, "deadline": None,
                                           "duration_min": None, "prefs": ["coffee"], "language": "ru",
                                           "via": [{"place": "ЭКСПО", "stay_min": None}]}}).json()
check("встреча: место друга, разобранное ИИ как «пункт по пути», всё равно даёт встречу посередине", bool(r.get("meet")))
cs = api.get("/api/city").json()
check("API: спрос для города — только счётчики, без координат и текстов",
      cs["requests"] >= 2 and cs["refusals"] >= 1 and "lat" not in str(cs) and "ЭКСПО" not in str(cs))

print(f"\n{ok} ок, {fail} провалено")
sys.exit(1 if fail else 0)
