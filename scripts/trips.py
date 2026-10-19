"""Сценарии передвижения «как пишут люди»: дом → дела по порядку → обратно. Гоняет живой сервер (:8010) с ИИ
и проверяет то, что можно проверить кодом: все пункты в маршруте и по порядку, конец там, где просили,
время выхода сдвинуто, часы работы, совет «такси или пешком», срок соблюдён.

    .venv/Scripts/python.exe -X utf8 scripts/trips.py          # все
    .venv/Scripts/python.exe -X utf8 scripts/trips.py 3 7      # только №3 и №7
"""
import re
import sys
import time

import httpx

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])
from app import geo  # noqa: E402

URL = "http://127.0.0.1:8010/api/plan"
HOME_BOOKMARK = {"Мой дом": {"lat": 51.1605, "lon": 71.4704, "kind": "home"}}   # условный дом на правом берегу

# text, now, ожидаемые пункты по порядку, ожидаемый конец (None — не проверяем; "start" — там же, где начал),
# закладки, сдвиг старта (мин), спросили ли «такси или пешком», крайний срок «HH:MM» (None — нет)
TRIPS = [
    ("Я дома в Мангилик Ел 53. Через полчаса выйду из дома, поеду в АИТУ, там поучусь пару часов, потом пойду в Мегу, "
     "там хочу что-то покушать, и потом обратно домой. Как лучше — на такси или пешком?",
     "10:35", ["АИТУ", "Mega Silk Way"], "start", {}, 30, True, None),
    ("Я у Байтерека. Сначала в Хан Шатыр, потом в Национальный музей, а потом домой",
     "12:00", ["Хан Шатыр", "Национальный музей"], "Мой дом", HOME_BOOKMARK, 0, False, None),
    ("Выезжаю через 20 минут из дома. Надо в НУ на час, потом в Керуен, к 18:00 вернуться домой",
     "13:00", ["Назарбаев", "Керуен"], "Мой дом", HOME_BOOKMARK, 20, False, "18:00"),
    ("Я на вокзале Нурлы Жол. Хочу в Мега Силк Вэй, потом к Байтереку, потом в аэропорт. Рейс в 21:00",
     "14:00", ["Mega Silk Way", "Байтерек"], "Аэропорт", {}, 0, False, "21:00"),
    ("С ЕНУ иду в Абу Даби Плазу, там час, потом в Хан Шатыр. Пешком или на такси?",
     "15:00", ["Абу Даби Плаза"], "Хан Шатыр", {}, 0, True, None),
    ("Я в Хан Шатыре. Потом хочу к Байтереку, потом в Ак Орду посмотреть, и обратно в Хан Шатыр",
     "11:00", ["Байтерек", "Ак Орда"], "Хан Шатыр", {}, 0, False, None),
    ("Из дома через час выйду, сначала в Мега Астану, потом в Керуен, потом домой",
     "16:00", ["MEGA Astana", "Керуен"], "Мой дом", HOME_BOOKMARK, 60, False, None),
    ("I'm at Baiterek. First Khan Shatyr, then the National Museum, then back to Baiterek. Taxi or walk?",
     "12:30", ["Хан Шатыр", "Национальный музей"], "Байтерек", {}, 0, True, None),
    ("Я у Астана Оперы, хочу в Национальный музей, потом в Дворец мира и согласия, потом домой. Есть 3 часа",
     "11:00", ["Национальный музей", "Дворец мира"], "Мой дом", HOME_BOOKMARK, 0, False, None),
    ("Мен АИТУ-дамын. Кейін Мегаға барамын, сосын үйге",
     "17:00", ["Mega Silk Way"], "Мой дом", HOME_BOOKMARK, 0, False, None),
    ("Я в Керуене. Через 15 минут выхожу, иду в Хан Шатыр, потом в Байтерек, потом обратно в Керуен. Как добираться — пешком или такси?",
     "18:00", ["Хан Шатыр", "Байтерек"], "Керуен", {}, 15, True, None),
    ("Я дома на Мангилик Ел 53. Потом в АИТУ к 9:00, после пар в Мегу пообедать, потом домой",
     "08:20", ["АИТУ", "Mega Silk Way"], "start", {}, 0, False, None),
    ("Я у Хазрет Султан. Сначала в Национальный музей, потом в Ак Орду, потом в Байтерек",
     "13:00", ["Национальный музей", "Ак Орда"], "Байтерек", {}, 0, False, None),
    ("Мы с другом у Абу Даби Плазы. Сначала в Мега Силк Вэй поесть, потом в АИТУ на встречу к 16:00",
     "13:30", ["Mega Silk Way"], "АИТУ", {}, 0, False, "16:00"),
]


def norm(s):
    return (s or "").lower().replace("ё", "е")


def match(name, want):
    """Пункт совпадает, если совпадает ключевое слово (АИТУ, Мега, Байтерек…) или точка ближе 600 м к ориентиру."""
    if norm(want).split()[0][:5] in norm(name):
        return True
    a, b = geo.find_place(want), geo.find_place(name)
    return bool(a and b and geo.haversine_km((a["lat"], a["lon"]), (b["lat"], b["lon"])) < 0.6)


def check(i, t):
    text, now, want, end, saved, shift, ask_mode, dl = t
    r = httpx.post(URL, json={"text": text, "now": now, "saved": saved, "weather": "now"}, timeout=180).json()
    issues = []
    if r.get("error"):
        return [f"отказ: {r['error'][:140]}"], r
    stops = [s for s in r["steps"] if s["type"] == "stop"]
    names = [s["name"] for s in stops]
    pos = []
    notes = " ".join(r.get("notes") or [])
    for w in want:
        k = next((j for j, n in enumerate(names) if match(n, w)), None)
        if k is None and re.search(r"убрал этот пункт|removed|алып таста", notes) and                 any(x and norm(x).split()[0][:5] in norm(notes) for x in (w, (geo.find_place(w) or {}).get("label_en"))):
            continue   # пункт закрыт (музей в понедельник) — честно убран с заметкой, это правильно
        if k is None:
            issues.append(f"нет пункта «{w}» (есть: {names})")
        pos.append(k)
    if None not in pos and pos != sorted(pos):
        issues.append(f"пункты не по порядку: {names}")
    extra = [n for n in names if not any(match(n, w) for w in want)]
    if extra:
        issues.append(f"лишние остановки: {extra}")
    dest = r.get("destination") or {}
    if end == "start":
        o = r.get("origin") or {}
        if geo.haversine_km((o["lat"], o["lon"]), (dest["lat"], dest["lon"])) > 0.3:
            issues.append(f"конец не там, откуда вышел: {dest.get('label')}")
    elif end == "Мой дом":
        if norm(dest.get("label")) not in ("мой дом", "домой"):
            issues.append(f"конец не дома: {dest.get('label')}")
    elif end and not match(dest.get("label"), end):
        issues.append(f"конец не тот: {dest.get('label')} вместо {end}")
    start = next(s for s in r["steps"] if s["type"] == "start")["time"]
    h, m = map(int, now.split(":"))
    exp = f"{(h * 60 + m + shift) // 60 % 24:02d}:{(h * 60 + m + shift) % 60:02d}"
    if start != exp:
        issues.append(f"старт {start}, а должен быть {exp}")
    for s in stops:
        if s["hours"]["status"] in ("closed", "closes_soon"):
            issues.append(f"{s['name']}: {s['hours']['text']}")
    if ask_mode and not re.search(r"такси|taxi", notes.lower()):
        issues.append("спросили «такси или пешком» — ответа нет")
    end_t = next(s for s in r["steps"] if s["type"] == "end")["time"]
    if dl and end_t > dl and not ("Впритык" in notes or "опозда" in notes.lower()):
        issues.append(f"срок {dl}, а прибытие {end_t} без предупреждения")
    legs = [s for s in r["steps"] if s["type"] == "leg"]
    if any(l["mode"] == "walk" and l["min"] > 45 for l in legs):
        issues.append("пеший кусок дольше 45 мин")
    return issues, r


def main():
    pick = {int(a) for a in sys.argv[1:]}
    bad = 0
    for i, t in enumerate(TRIPS, 1):
        if pick and i not in pick:
            continue
        t0 = time.time()
        issues, r = check(i, t)
        bad += bool(issues)
        route = " → ".join([(r.get("origin") or {}).get("label", "?")] +
                           [f"{s['name']} {s['arrive']}-{s['leave']}" for s in r.get("steps", []) if s["type"] == "stop"] +
                           [(r.get("destination") or {}).get("label", "?")])
        print(f"{i:2} {'OK ' if not issues else 'ERR'} {time.time() - t0:4.1f}s  {t[0][:70]}")
        print(f"      {route[:230]}")
        for x in issues:
            print(f"      ✗ {x}")
    n = len(pick) or len(TRIPS)
    print(f"\nИТОГ: {n - bad}/{n} без замечаний")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
