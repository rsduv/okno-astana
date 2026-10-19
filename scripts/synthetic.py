"""Синтетический набор проверочных запросов: 120 разных «живых» фраз (5 сценариев из ТЗ + маршруты, группы,
доступность, мороз, ночь, 3 языка, невыполнимые просьбы). Для каждой записан ожидаемый тип ответа. Скрипт
гоняет их через живой сервер (:8010, с ИИ) и проверяет кодом то, что проверяется однозначно.

    .venv/Scripts/python.exe -X utf8 scripts/synthetic.py            # сгенерировать + прогнать все
    .venv/Scripts/python.exe -X utf8 scripts/synthetic.py gen        # только сгенерировать набор
    .venv/Scripts/python.exe -X utf8 scripts/synthetic.py 5 17       # прогнать только №5 и №17
Результат: data/dataset/synthetic_queries.jsonl (вход + ожидание), data/dataset/synthetic_results.jsonl (ответы),
docs/synthetic_tests.md (итог по сценариям, провалы, примеры).
"""
import json
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import geo  # noqa: E402

URL = "http://127.0.0.1:8010/api/plan"
OUT = ROOT / "data" / "dataset"
SEED = 2026

PLACES = ["Хан Шатыр", "Mega Silk Way", "Байтерек", "ЭКСПО", "Керуен", "Абу Даби Плаза", "Астана Опера",
          "Хазрет Султан", "Национальный музей", "ЕНУ", "АИТУ", "вокзал Нурлы Жол", "Ак Орда", "Дворец мира и согласия",
          "Назарбаев Университет", "MEGA Astana", "Президентский парк", "Атамекен", "Триумфальная арка", "Астана Арена"]
EN = {"Хан Шатыр": "Khan Shatyr", "Mega Silk Way": "Mega Silk Way", "Байтерек": "Baiterek", "ЭКСПО": "EXPO",
      "Керуен": "Keruen mall", "Абу Даби Плаза": "Abu Dhabi Plaza", "Астана Опера": "Astana Opera",
      "Национальный музей": "the National Museum", "вокзал Нурлы Жол": "Nurly Zhol station", "Ак Орда": "Ak Orda"}
KK = {"Хан Шатыр": "Хан Шатыр", "Байтерек": "Бәйтерек", "ЭКСПО": "ЭКСПО", "Керуен": "Керуен", "Mega Silk Way": "Мега Силк Уэй"}

# группа категорий, которую ждём увидеть хотя бы в одной остановке
WANT = {
    "coffee": {"кафе", "коворкинг"}, "work": {"кафе", "коворкинг", "библиотека"},
    "kids": {"детская площадка", "детский центр", "развлечения", "музей", "парк", "океанариум", "батутный парк",
             "парк развлечений", "аквапарк", "торговый центр", "кино"},
    "night": {"бар", "ночной клуб", "кино", "боулинг", "квест", "развлечения", "ресторан", "кафе", "достопримечательность",
              "памятник", "парк", "смотровая", "театр", "фонтан"},
    "food": {"ресторан", "кафе", "фастфуд", "торговый центр"},
    "culture": {"музей", "галерея", "арт-центр", "театр", "библиотека", "достопримечательность", "памятник", "мечеть", "собор"},
    "nature": {"парк", "сад", "фонтан", "достопримечательность", "памятник", "смотровая"},
}

# (сценарий, шаблоны, ожидание). {P} — место, {N} — минуты, {H} — часы
FAMILIES = [
    ("прогулка рядом (ТЗ №1)", [
        "Я сейчас возле {P}, хочу прогуляться {N} минут — что посоветуете?",
        "у {P}, есть {N} мин погулять, куда сходить",
        "Стою у {P}. Свободно {N} минут, хочу пройтись и что-нибудь посмотреть",
        "я около {P} кароч {N} минут свободных есть чем заняться?",
    ], {"stops": (1, 3), "want": None, "window": True}),
    ("тихое кафе с розеткой (ТЗ №2)", [
        "Хочу тихое место с кофе и розеткой недалеко от {P}",
        "Нужна кофейня с розеткой и вайфаем рядом с {P}, поработать {H} часа",
        "где рядом с {P} можно спокойно посидеть с ноутбуком и кофе",
        "посоветуй тихую кофейню у {P}, надо позаниматься",
    ], {"stops": (1, 3), "want": "coffee", "window": False}),
    ("с ребёнком (ТЗ №3)", [
        "Еду с ребёнком к {P} — где рядом детские площадки или музей с интерактивами?",
        "Мы с сыном 6 лет у {P}, есть {N} минут, куда сходить?",
        "С дочкой у {P}, погода норм, где поиграть {N} минут",
        "с детьми возле {P}, чем занять малыша на час",
    ], {"stops": (1, 3), "want": "kids", "window": False}),
    ("турист перед вылетом (ТЗ №4)", [
        "Я турист, у меня {H} часа перед вылетом, сейчас у {P}: что можно посмотреть поблизости?",
        "Рейс через {H} часа, я у {P}. Что успею посмотреть и не опоздать в аэропорт?",
        "транзит, {H} часа до самолёта, я в {P}. Покажи главное",
    ], {"stops": (1, 3), "want": None, "window": False, "airport": True}),
    ("ночная активность (ТЗ №5)", [
        "Ищу необычную ночную активность на последние пару часов, я у {P}",
        "Сейчас ночь, я у {P}, куда сходить часа на два?",
        "что поделать ночью возле {P}, не хочу домой",
    ], {"stops": (1, 3), "want": "night", "window": False, "time": "night"}),
    ("поесть", [
        "Голодный у {P}, где нормально поесть за {N} минут?",
        "Хочу поужинать недалеко от {P}, есть {N} мин",
        "где перекусить рядом с {P} быстро",
        "посоветуй казахскую кухню рядом с {P}",
    ], {"stops": (1, 3), "want": "food", "window": False}),
    ("культура", [
        "Хочу в музей или на выставку, я у {P}, есть {H} часа",
        "Что посмотреть из культурного рядом с {P}? {N} минут",
        "я у {P}, интересует архитектура и история",
    ], {"stops": (1, 3), "want": "culture", "window": False, "time": "day"}),
    ("маршрут А→Б с окном", [
        "Я у {P}, к {T} мне надо быть в {Q}. По пути хочу кофе",
        "Иду из {P} в {Q}, есть {N} минут, что по дороге посмотреть?",
        "из {P} в {Q} к {T}, хочу пройтись и где-то перекусить",
    ], {"stops": (1, 3), "want": None, "window": True, "dest": True}),
    ("группа друзей", [
        "Нас 4 человека у {P}, хотим посидеть где-нибудь {N} минут",
        "Мы компанией из 5 человек у {P}, куда сходить вечером?",
        "Нас трое, мы у {P}, есть {H} часа, хотим погулять и поесть",
    ], {"stops": (1, 3), "want": None, "window": False}),
    ("доступность", [
        "Я на коляске, нахожусь у {P}, есть {N} минут. Куда можно спокойно доехать?",
        "Бабушке 75 лет, тяжело ходить, мы у {P}. Куда сходить ненадолго?",
        "У меня ребёнок в коляске, я у {P}, погулять {N} минут без лестниц",
    ], {"stops": (1, 3), "want": None, "window": False, "mobility": True}),
    ("мороз −25°", [
        "Холодно, я у {P}, есть {N} минут. Куда пойти, чтобы не замёрзнуть?",
        "Мороз, ветер, я у {P}, хочу погреться и выпить чаю",
        "−25 на улице, у {P}, {N} минут убить",
    ], {"stops": (1, 3), "want": None, "window": False, "cold": True}),
    ("английский", [
        "I'm near {E}, I have {N} minutes. Where should I go?",
        "Tourist here, at {E}, flight in {H} hours. What can I see nearby?",
        "Looking for a quiet cafe with a socket near {E}",
        "At {E} with kids, what to do for an hour?",
    ], {"stops": (1, 3), "want": None, "window": False, "lang": "en"}),
    ("казахский", [
        "Мен {K} жанындамын, {N} минут бар. Қайда барайын?",
        "{K} маңында тыныш кофехана керек",
        "Баламен {K} жанындамыз, қайда ойнауға болады?",
    ], {"stops": (1, 3), "want": None, "window": False, "lang": "kk"}),
    ("цепочка дел", [
        "Я у {P}. Сначала в {Q}, потом в {R}, потом обратно",
        "Из {P} в {Q} на час, потом в {R}. Такси или пешком?",
    ], {"stops": (1, 3), "want": None, "window": False, "chain": True}),
    ("край: невыполнимо или почти", [
        "Я у {P}, есть 5 минут, куда сходить?",
        "Хочу в музей в 3 часа ночи, я у {P}",
        "Я у {P}, через 10 минут надо быть в аэропорту, по пути кофе",
    ], {"stops": (0, 1), "want": None, "window": False, "edge": True}),
]


def gen(n=120):
    rnd = random.Random(SEED)
    rows, i = [], 0
    while len(rows) < n:
        fam, tpls, exp = FAMILIES[i % len(FAMILIES)]
        i += 1
        tpl = rnd.choice(tpls)
        p, q, r = rnd.sample(PLACES, 3)
        if "{E}" in tpl:
            p = rnd.choice(list(EN))
        if "{K}" in tpl:
            p = rnd.choice(list(KK))
        N = rnd.choice([30, 40, 45, 60, 90])
        H = rnd.choice([2, 3])
        if exp.get("dest"):
            N = rnd.choice([60, 90, 120])   # А→Б через полгорода за 30 мин честно не успеть — это не то, что проверяем
        hour = {"night": rnd.choice([22, 23, 0, 1]), "day": rnd.choice([11, 12, 14, 15])}.get(exp.get("time"),
                                                                                              rnd.choice([9, 11, 13, 15, 17, 19]))
        now = f"{hour:02d}:{rnd.choice([0, 10, 20, 30, 40, 50]):02d}"
        T_h = (hour * 60 + int(now[3:]) + N) // 60 % 24
        T = f"{T_h:02d}:{(int(now[3:]) + N) % 60 // 10 * 10:02d}"
        text = tpl.format(P=p, Q=q, R=r, N=N, H=H, T=T, E=EN.get(p, p), K=KK.get(p, p))
        if text in {x["text"] for x in rows}:
            continue
        e = {k: v for k, v in exp.items()}
        e["stops"] = list(e["stops"])
        if e.pop("window") and "{N}" in tpl and "{T}" not in tpl:
            e["window_min"] = N
        if "{T}" in tpl:
            e["deadline"] = T
        if e.get("dest"):
            e["dest"] = q
        if fam == "английский":
            e["want"] = "coffee" if "cafe" in text else ("kids" if "kids" in text else None)
        if fam == "казахский":
            e["want"] = "coffee" if "кофе" in text else ("kids" if "Бала" in text else None)
        if e.get("chain"):
            e["via"] = [q, r]
            e["stops"] = [1, 2]
        rows.append({"id": len(rows) + 1, "family": fam, "text": text, "now": now,
                     "weather": "cold" if e.get("cold") else "now", "origin": p, "expect": e,
                     "expected_answer": expected_text(fam, e)})
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "synthetic_queries.jsonl", "w", encoding="utf-8") as f:
        for x in rows:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    return rows


def expected_text(fam, e):
    """Человеческое описание ожидаемого ответа — как в ТЗ «входные данные + ожидаемый тип ответа»."""
    if e.get("edge"):
        return "Честно: не помещается / закрыто — и что можно вместо (0–1 место), без выдуманного «открыто»"
    want = {"coffee": "кафе/коворкинг", "kids": "место для ребёнка", "night": "открытое ночью", "food": "где поесть",
            "culture": "музей/галерея/архитектура", None: "по смыслу запроса"}[e.get("want")]
    s = f"1–3 места ({want}), у каждого: почему, сколько времени, что делать, часы с уверенностью и источником"
    if e.get("window_min"):
        s += f"; весь маршрут укладывается в {e['window_min']} мин"
    if e.get("deadline"):
        s += f"; на месте к {e['deadline']}"
    if e.get("airport"):
        s += "; учтена дорога в аэропорт и запас до вылета"
    if e.get("cold"):
        s += "; в мороз — минимум улицы, тёплые места"
    if e.get("mobility"):
        s += "; короткие переходы, без длинных пеших кусков"
    if e.get("lang"):
        s += f"; ответ на языке запроса ({e['lang']})"
    if e.get("chain"):
        s += "; названные пункты в том же порядке"
    return s


def mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def check(q, r):
    e, bad = q["expect"], []
    if "detail" in r:
        return [f"ошибка запроса: {str(r['detail'])[:120]}"]
    if r.get("error"):
        # отказ с конкретным советом («успеешь одно: фонтан, 14 мин», «лучше ехать сразу») — это полезный ответ, а не сбой
        if r.get("tip") or re.search(r"Успеешь одно|с собой|лучше ехать сразу|Просто иди|не успеть|go straight|takeaway", r["error"]):
            return bad
        if not e.get("edge") and not (e.get("dest") and re.search(r"не успе", r["error"])):
            bad.append(f"отказ: {r['error'][:110]}")
        return bad
    stops = [s for s in r["steps"] if s["type"] == "stop"]
    lo, hi = e["stops"]
    if not (lo <= len(stops) <= max(hi, 3)):
        bad.append(f"остановок {len(stops)}, ждали {lo}–{hi}")
    for s in stops:
        if not (s.get("why") or "").strip() or not (s.get("todo") or "").strip():
            bad.append(f"{s['name']}: нет «почему» или «что делать»")
        if not s.get("stay"):
            bad.append(f"{s['name']}: нет времени на месте")
        h = s.get("hours") or {}
        if h.get("status") in ("closed", "closes_soon"):
            bad.append(f"{s['name']}: {h.get('text')}")
        if not h.get("confidence"):
            bad.append(f"{s['name']}: часы без степени уверенности")
    if not r.get("data_used"):
        bad.append("нет блока «какие данные использованы»")
    start = mins(r["now"][-5:]) if re.match(r".*\d\d:\d\d$", r.get("now") or "") else mins(q["now"])
    arrive = mins(r["summary"]["arrive"])
    span = (arrive - start) % 1440
    if e.get("window_min") and span > e["window_min"] + 2:
        bad.append(f"не уложился: {span} мин из {e['window_min']}")
    if e.get("deadline") and (arrive - mins(e["deadline"])) % 1440 not in range(0, 3) and (mins(e["deadline"]) - arrive) % 1440 > 720:
        if not re.search(r"Впритык|не успе|опозда", " ".join(r.get("notes") or [])):
            bad.append(f"срок {e['deadline']}, на месте в {r['summary']['arrive']} без предупреждения")
    if e.get("want") and stops and not any(s["category"] in WANT[e["want"]] for s in stops):
        bad.append(f"нет места типа «{e['want']}»: {[s['category'] for s in stops]}")
    if e.get("cold") and r["summary"].get("max_outdoor_leg", 0) > 20:
        bad.append(f"мороз, а пеший кусок {r['summary']['max_outdoor_leg']} мин")
    if e.get("mobility"):
        legs = [s for s in r["steps"] if s["type"] == "leg" and s["mode"] == "walk"]
        if any(x["min"] > 15 for x in legs):
            bad.append("маломобильному — пеший кусок дольше 15 мин")
    if e.get("dest"):
        a, b = geo.find_place(e["dest"]), r.get("destination") or {}
        if a and b.get("lat") and geo.haversine_km((a["lat"], a["lon"]), (b["lat"], b["lon"])) > 1.2:
            bad.append(f"конец не там: {b.get('label')} вместо {e['dest']}")
    txt = " ".join([r.get("headline") or ""] + [s.get("why", "") for s in stops])
    if e.get("lang") == "en" and re.search(r"[а-яё]{4,}", txt.lower()):
        bad.append("ответ не на английском")
    if e.get("lang") == "kk" and not re.search(r"[әіңғүұқөһ]", txt.lower()):
        bad.append("ответ не на казахском")
    if e.get("chain"):
        notes = " ".join(r.get("notes") or [])
        for v in e["via"]:
            pt = geo.find_place(v)
            pts = stops + [r.get("destination") or {}]   # последний пункт цепочки может быть финишем
            ok = pt and any(x.get("lat") and geo.haversine_km((pt["lat"], pt["lon"]), (x["lat"], x["lon"])) < 0.8 for x in pts)
            if not ok and not re.search(r"убрал|закрыт|выходной", notes):
                bad.append(f"нет пункта «{v}»")
    return bad


def run_one(q):
    t0 = time.time()
    try:
        r = httpx.post(URL, json={"text": q["text"], "now": q["now"], "weather": q["weather"], "saved": {}}, timeout=180).json()
    except Exception as ex:   # noqa: BLE001
        r = {"error": f"сервер не ответил: {ex}"}
    return q, r, check(q, r), round(time.time() - t0, 1)


def brief(r):
    if r.get("error"):
        return "Отказ: " + r["error"][:160]
    stops = [s for s in r["steps"] if s["type"] == "stop"]
    return "; ".join(f"{s['name']} ({s['category']}, {s['arrive']}–{s['leave']}): {s.get('why', '')[:90]}" for s in stops)


def report(res):
    fam = defaultdict(lambda: [0, 0])
    for q, r, bad, _ in res:
        fam[q["family"]][0] += 1
        fam[q["family"]][1] += not bad
    n, ok = len(res), sum(1 for x in res if not x[2])
    sec = sorted(x[3] for x in res)
    lines = ["# Синтетические проверочные запросы", "",
             f"**{ok} из {n}** запросов прошли все автоматические проверки ({ok / n:.0%}). "
             f"Время ответа: медиана {sec[len(sec) // 2]} с, 90% быстрее {sec[int(len(sec) * 0.9)]} с.", "",
             "Набор сгенерирован скриптом `scripts/synthetic.py` (фиксированный seed — можно повторить): 15 типов запросов — "
             "5 сценариев из ТЗ, маршрут А→Б, группы, доступность, мороз, три языка, цепочки дел и заведомо невыполнимые просьбы. "
             "Фразы написаны «как пишут люди» — с опечатками, сленгом, без знаков препинания. Вход и ожидаемый тип ответа — "
             "`data/dataset/synthetic_queries.jsonl`, ответы системы — `data/dataset/synthetic_results.jsonl`.", "",
             "## Что проверяется кодом (без ИИ-судьи)", "",
             "- 1–3 места; у каждого есть «почему», «что делать», время на месте;",
             "- ни одно место не закрыто на время визита; у часов есть степень уверенности и источник;",
             "- маршрут укладывается в названное окно / к названному сроку (или есть честное предупреждение);",
             "- тип места совпадает с просьбой (кофе → кафе, ребёнок → детское, ночь → открытое ночью…);",
             "- мороз → короткие уличные куски; коляска/пожилые → пешие куски ≤ 15 мин;",
             "- английский/казахский запрос → ответ на том же языке; цепочка дел → все пункты по порядку;",
             "- невыполнимая просьба → честный отказ с подсказкой, а не выдуманный маршрут;",
             "- в ответе есть блок «какие данные использованы».", "",
             "## Итог по типам запросов", "", "| Тип запроса | Прошли |", "|---|---|",
             *[f"| {k} | {v[1]}/{v[0]} |" for k, v in fam.items()], ""]
    fails = [x for x in res if x[2]]
    if fails:
        lines += ["## Не прошли (честно)", ""]
        for q, r, bad, _ in fails:
            lines.append(f"- **№{q['id']}** «{q['text']}» ({q['now']}) — " + "; ".join(bad))
        lines.append("")
    lines += ["## Примеры (вход → ожидание → ответ)", "", "| № | Запрос | Время | Ожидаем | Ответ «Окна» |", "|---|---|---|---|---|"]
    seen = set()
    for q, r, bad, _ in res:
        if q["family"] in seen or bad:
            continue
        seen.add(q["family"])
        lines.append(f"| {q['id']} | {q['text']} | {q['now']}{' · −25°' if q['weather'] == 'cold' else ''} | "
                     f"{q['expected_answer']} | {brief(r).replace('|', '/')} |")
    (ROOT / "docs" / "synthetic_tests.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    args = sys.argv[1:]
    rows = gen()
    if args[:1] == ["gen"]:
        print(f"{len(rows)} запросов → data/dataset/synthetic_queries.jsonl")
        return
    pick = {int(a) for a in args}
    todo = [q for q in rows if not pick or q["id"] in pick]
    res = []
    with ThreadPoolExecutor(4) as ex:
        for q, r, bad, sec in ex.map(run_one, todo):
            res.append((q, r, bad, sec))
            print(f"{q['id']:3} {'OK ' if not bad else 'ERR'} {sec:5}s  {q['text'][:70]}", flush=True)
            for b in bad:
                print("       ✗", b)
    ok = sum(1 for x in res if not x[2])
    print(f"\nИТОГ: {ok}/{len(res)}")
    if not pick:
        with open(OUT / "synthetic_results.jsonl", "w", encoding="utf-8") as f:
            for q, r, bad, sec in res:
                f.write(json.dumps({"id": q["id"], "text": q["text"], "now": q["now"], "ok": not bad, "issues": bad,
                                    "seconds": sec, "answer": brief(r), "notes": (r.get("notes") or [])[:3]},
                                   ensure_ascii=False) + "\n")
        report(res)


if __name__ == "__main__":
    main()
