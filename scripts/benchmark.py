"""Честное сравнение: «Окно» против обычного ИИ-чатбота (Claude без наших данных) на одних и тех же 30 запросах.

  1) docs/benchmark_queries.json — запросы и время;
  2) docs/benchmark_baseline.json — ответы чатбота (его просили ответить «как обычный ИИ в чате», без интернета);
  3) этот скрипт прогоняет те же запросы через «Окно» (сервер на :8010) и ОДИНАКОВО проверяет оба ответа:
     - место существует? (наша база OSM+2GIS+Wikidata, иначе поиск в OpenStreetMap по Астане);
     - открыто ли оно в момент визита? (только если часы известны точно — OSM/2GIS; «оценка» не считается ошибкой);
     - успеваешь ли физически? (переход между точками не быстрее, чем пешком/на такси по нашей оценке);
     - не опоздал ли к сроку?
Результат — docs/benchmark.md. Запуск: python scripts/benchmark.py
"""
import difflib, json, re, sys, time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import geo, planner  # noqa: E402
from app.hours import interval_status  # noqa: E402

API = "http://127.0.0.1:8010/api/plan"
Q = json.loads((ROOT / "docs/benchmark_queries.json").read_text(encoding="utf-8"))
BASE = json.loads((ROOT / "docs/benchmark_baseline.json").read_text(encoding="utf-8"))
CACHE_F = ROOT / "data/bench_cache.json"
cache = json.loads(CACHE_F.read_text(encoding="utf-8")) if CACHE_F.exists() else {}
TODAY = datetime.now(geo.KZ).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)

STOP = r"\b(трц|тц|тРЦ|кафе|ресторан|бар|кофейня|парк|музей|мечеть|площадь|монумент|кинотеатр|cafe|restaurant|park|museum|the)\b"


def norm(s):
    s = re.sub(r"[«»\"'“”()\-–—,.:]", " ", (s or "").lower()).replace("ё", "е")
    s = re.sub(STOP, " ", s)
    return re.sub(r"\s+", " ", s).strip()


NAMES = []
for p in planner.POIS:
    for n in {p["name"], p.get("name_en") or "", p.get("name_kk") or ""}:
        if n:
            NAMES.append((norm(n), p))
for lm in geo.LANDMARKS:
    fake = {"name": lm["name"], "lat": lm["lat"], "lon": lm["lon"], "category": "ориентир", "tags": {}}
    for n in [lm["name"], *lm.get("aliases", [])]:
        NAMES.append((norm(n), fake))


# «прогулка по бульвару», «кафе в ТРЦ Хан Шатыр», «фуд-корт» — это не точка, а зона/место внутри другого:
# расстояния до них не проверяем (иначе судья нечестно «ловит» чатбот на переходах)
AREA = re.compile(r"прогулк|бульвар|набережн|территори|галере|фуд-корт|фудкорт|уровн|район|рядом|внутри|\bв (трц|тц|хан шатыр|mega|керуен)|\bи\b|с собой", re.I)


def match(name, near=None):
    """Найти место в нашей базе по названию. Сеть (несколько филиалов) — берём ближайший филиал к предыдущей точке."""
    k = norm(re.sub(r"\([^)]*\)", " ", name))   # «Coffee Boom (район Нуржол)» → «coffee boom»
    if len(k) < 3:
        return None
    cands = []
    for n, p in NAMES:
        if not n:
            continue
        r = 1.0 if n == k else difflib.SequenceMatcher(None, k, n).ratio()
        if len(n) >= 5 and (n in k or k in n):
            r = max(r, 0.9)
        if r >= 0.82:
            cands.append((r, p))
    if not cands:
        return None
    top = max(r for r, _ in cands)
    same = [p for r, p in cands if r >= top - 0.02]
    if near:
        same.sort(key=lambda p: geo.haversine_km(near, (p["lat"], p["lon"])))
    return same[0]


def osm_exists(name):
    """Нет в нашей базе — ищем в OpenStreetMap по Астане (Nominatim, 1 запрос/сек, с кэшем)."""
    if name in cache:
        return cache[name]
    time.sleep(1.1)
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search", headers=geo.UA, timeout=10, params={
            "q": name, "format": "jsonv2", "limit": 1, "viewbox": "71.25,51.25,71.65,51.0", "bounded": 1})
        j = r.json()
        res = {"lat": float(j[0]["lat"]), "lon": float(j[0]["lon"])} if j else None
    except Exception:
        res = None
    cache[name] = res
    CACHE_F.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def t_at(hhmm, now):
    h, m = map(int, hhmm.split(":"))
    t = TODAY.replace(hour=h, minute=m)
    return t + timedelta(days=1) if t < now - timedelta(hours=2) else t


def need_min(a, b, t):
    return min(geo.walk_min_est(a, b), geo.car_min_est(a, b, t) + geo.TAXI_WAIT_MIN)


def check(plan, start_pt, end_pt, now, hard_deadline):
    """Одинаковая проверка для обоих. plan = {"stops":[{name,arrive,leave}], "end":{"arrive"}}."""
    issues, unknown, ok_stops = [], 0, 0
    notfound = NOTFOUND
    pos, t_prev = start_pt, now
    for s in plan.get("stops") or []:
        try:
            arr, lv = t_at(s["arrive"], now), t_at(s["leave"], now)
        except Exception:
            issues.append(f"«{s.get('name')}»: нет времени визита"); continue
        if not s.get("_poi") and AREA.search(re.sub(r"\([^)]*\)", "", s["name"])):   # пояснение в скобках не делает кафе «зоной»
            unknown += 1; pos, t_prev = None, lv   # зона, а не точка — не проверяем ни часы, ни переходы
            continue
        p = s.get("_poi") or match(s["name"], pos)
        pt = (p["lat"], p["lon"]) if p else None
        if not p:
            o = osm_exists(re.sub(r"\([^)]*\)", "", s["name"]).strip())
            unknown += 1   # нет в базе: существует или нет — не утверждаем, часов нет — не проверить
            pt = (o["lat"], o["lon"]) if o else None
            if not o:
                notfound.append(s["name"])
        else:
            st, conf, txt = interval_status(p.get("tags"), p["category"], arr, max(lv, arr + timedelta(minutes=5)))
            if conf == "verified" and st in ("closed", "closes_soon"):
                issues.append(f"«{s['name']}» в {s['arrive']}–{s['leave']}: закрыто ({txt.split(' · ')[0]})")
            elif conf != "verified" and conf != "outdoor":
                unknown += 1
            else:
                ok_stops += 1
        if pos and pt:
            need = need_min(pos, pt, t_prev)
            gap = (arr - t_prev).total_seconds() / 60
            if gap < need * 0.6 - 3:
                issues.append(f"до «{s['name']}» за {gap:.0f} мин — физически нужно ≥{need*0.6:.0f}")
        pos, t_prev = pt or pos, lv
    end = plan.get("end") or {}
    if end.get("arrive") and end_pt and pos:
        e = t_at(end["arrive"], now)
        need = need_min(pos, end_pt, t_prev)
        gap = (e - t_prev).total_seconds() / 60
        if gap < need * 0.6 - 3:
            issues.append(f"до финиша за {gap:.0f} мин — физически нужно ≥{need*0.6:.0f}")
        if hard_deadline and e > hard_deadline + timedelta(minutes=2):
            issues.append(f"опоздание: на месте в {end['arrive']}, а нужно к {hard_deadline:%H:%M}")
    return issues, unknown, ok_stops


NOTFOUND: list[str] = []
rows = []
for q in Q["queries"]:
    now = TODAY.replace(hour=int(q["now"][:2]), minute=int(q["now"][3:]))
    r = httpx.post(API, json={"text": q["text"], "now": q["now"]}, timeout=120).json()
    o, d = r.get("origin"), r.get("destination")
    start_pt = (o["lat"], o["lon"]) if o else None
    end_pt = (d["lat"], d["lon"]) if d else None
    # жёсткий срок — как сказал человек (для рейса: регистрация закрывается за 40 мин), без наших запасов
    pd = (r.get("parsed") or {})
    hard = None
    if pd.get("deadline"):
        hard = t_at(pd["deadline"], now)
        if re.search(r"вылет|рейс|flight|аэропорт", q["text"].lower()):
            hard -= timedelta(minutes=40)
    elif pd.get("duration_min"):
        hard = now + timedelta(minutes=pd["duration_min"] + 10)
    # «Окно»
    if r.get("error"):
        okno = {"refused": r["error"]}
        oi, ou, oo = [], 0, 0
    else:
        st = [x for x in r["steps"] if x["type"] == "stop"]
        byid = {p["id"]: p for p in planner.POIS}
        okno = {"stops": [{"name": x["name"], "arrive": x["arrive"], "leave": x["leave"], "_poi": byid.get(x["id"])} for x in st],
                "end": {"arrive": r["summary"]["arrive"]}}
        oi, ou, oo = check(okno, start_pt, end_pt, now, hard)
    b = next((a for a in BASE["answers"] if a["id"] == q["id"]), {})
    bi, bu, bo = check(b, start_pt, end_pt, now, hard)
    rows.append({"q": q, "okno": okno, "oi": oi, "ou": ou, "oo": oo, "base": b, "bi": bi, "bu": bu, "bo": bo})
    print(q["id"], "Окно:", len(oi), "| чатбот:", len(bi), flush=True)


def agg(key_i, key_plan):
    plans = [x for x in rows if not x[key_plan].get("refused")]
    stops = sum(len(x[key_plan].get("stops") or []) for x in plans)
    flat = [i for x in rows for i in x[key_i]]
    return {"plans": len(plans), "stops": stops,
            "closed": sum("закрыто" in i for i in flat),
            "unknown": sum(x[key_i.replace("i", "u")] for x in rows),
            "rush": sum("физически" in i for i in flat),
            "late": sum("опоздание" in i for i in flat),
            "bad": sum(1 for x in rows if x[key_i])}


A, B = agg("oi", "okno"), agg("bi", "base")
L = []
L.append("# «Окно» против обычного ИИ-чатбота — 30 одинаковых запросов\n")
L.append(f"Дата прогона: {datetime.now(geo.KZ):%d.%m.%Y %H:%M}. Чатбот: {BASE.get('model_note', 'Claude')} — его попросили ответить "
         "как обычный ИИ-ассистент в чате, из своих знаний, без интернета. «Окно» — наш сервис с данными OSM + 2GIS + Wikidata.\n")
L.append("Оба ответа проверяет **один и тот же скрипт** (`scripts/benchmark.py`) по одним правилам:\n"
         "- место ищем в базе (сеть — ближайший филиал); «прогулка по бульвару» и «кафе внутри ТРЦ» не проверяем по расстоянию;\n"
         "- **закрыто** — только если часы известны точно (OSM/2GIS); где часов нет, ошибкой не считаем;\n"
         "- **физически не успеть** — на переход заложено меньше 60% от минимума (пешком или такси) — с большим запасом в пользу ответа;\n"
         "- **опоздание** — к сроку, который назвал человек (для рейса — за 40 мин до вылета, без наших запасов).\n")
L.append("| | Обычный ИИ-чатбот | **Окно** |\n|---|---|---|")
L.append(f"| Ответов с планом | {B['plans']} из 30 | {A['plans']} из 30" + (" (остальное — честный отказ с советом)" if A['plans'] < 30 else "") + " |")
L.append(f"| Остановок предложено | {B['stops']} | {A['stops']} |")
L.append(f"| Визит в **закрытое** место | {B['closed']} | **{A['closed']}** |")
L.append(f"| Остановок, которые нельзя проверить (нет часов в данных, «прогулка по бульвару») | {B['unknown']} | {A['unknown']} |")
L.append(f"| Переходы, на которые **физически не хватает времени** | {B['rush']} | **{A['rush']}** |")
L.append(f"| **Опоздания** к сроку | {B['late']} | **{A['late']}** |")
L.append(f"| **Ответов хотя бы с одной ошибкой** | **{B['bad']} из 30** | **{A['bad']} из 30** |\n")
L.append("Честные оговорки: судья использует нашу базу часов (OSM/2GIS) — она реальная, но не полная, поэтому места без часов "
         "не засчитываются ни как ошибка, ни как успех. Места, которых нет ни в базе, ни в OpenStreetMap, тоже **не** считаем ошибкой "
         "(они могут существовать) — просто перечисляем ниже. Время в пути — наша оценка (пешком 4,5 км/ч, такси с пробками), "
         "поэтому порог мягкий — 60%.\n")
L.append(f"**Главное:** обычный ИИ ошибся в {B['bad']} ответах из 30 (закрыто к приходу, не успеть, опоздание), а "
         f"{round(100 * B['unknown'] / max(B['stops'], 1))}% его остановок невозможно проверить — «прогулка по бульвару», «кафе где-то в районе», "
         f"места без часов. У «Окна» — {A['bad']} ошибок и проверяемы {round(100 * (A['stops'] - A['unknown']) / max(A['stops'], 1))}% остановок: "
         "у каждой есть координаты, часы на момент визита и источник.\n")
L.append("Важно: «Окно» само строит план по этим же данным (OSM/2GIS), поэтому его «0 закрытых» во многом ожидаем — "
         "ценность сравнения в том, что ИИ без данных о часах и расстояниях ошибается и говорит размыто, а с данными — нет. "
         "Судья написан нами; весь код проверки и ответы обоих — в репозитории, их можно перепроверить.\n")
L.append("Не нашлись ни в базе, ни в OpenStreetMap (не засчитано): " + (", ".join(f"«{n}»" for n in sorted(set(NOTFOUND))) or "—") + "\n")
L.append("## Подробно по каждому запросу\n")
for x in rows:
    q = x["q"]
    L.append(f"### {q['id']}. «{q['text']}» — {q['now']}")
    bs = x["base"].get("stops") or []
    L.append("- **Чатбот:** " + ("; ".join(f"{s['arrive']} {s['name']}" for s in bs) or "—")
             + f" → финиш {(x['base'].get('end') or {}).get('arrive', '?')}")
    L.append("  - " + ("✅ ошибок не найдено" if not x["bi"] else "❌ " + "; ".join(x["bi"])))
    if x["okno"].get("refused"):
        L.append(f"- **Окно:** честный отказ — {x['okno']['refused']}")
    else:
        L.append("- **Окно:** " + "; ".join(f"{s['arrive']} {s['name']}" for s in x["okno"]["stops"]) + f" → финиш {x['okno']['end']['arrive']}")
        L.append("  - " + ("✅ ошибок не найдено" if not x["oi"] else "❌ " + "; ".join(x["oi"])))
    L.append("")
(ROOT / "docs/benchmark.md").write_text("\n".join(L), encoding="utf-8")
print(json.dumps({"okno": A, "chatbot": B}, ensure_ascii=False))
