"""«Тайные покупатели» для «Окна»: ИИ придумывает жителей и гостей Астаны, каждый спрашивает «Окно»,
отдельный судья (код + ИИ) проверяет, получил ли человек то, что хотел. Запускается по кругу: нашли провалы → починили → снова.

  python scripts/personas.py gen [N]   — придумать N персонажей → docs/personas.json (по умолчанию 60)
  python scripts/personas.py run       — прогнать всех через сервер :8010 и оценить → docs/personas_report.md
  python scripts/personas.py run 12    — только персонаж №12 (для отладки)
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import geo, llm, planner  # noqa: E402
from app.hours import interval_status  # noqa: E402

API = "http://127.0.0.1:8010/api/plan"
FILE = ROOT / "docs" / "personas.json"
REPORT = ROOT / "docs" / "personas_report.md"
LANDMARKS = [l["name"] for l in geo.LANDMARKS]

GEN = """Придумай %d разных РЕАЛЬНЫХ людей, которые прямо сейчас открыли в Астане сервис «Окно» (помощник по свободному времени:
подбирает 1–3 места по пути так, чтобы человек успел к сроку, или считает маршрут по пунктам, которые человек назвал сам).
Нужно разнообразие: студенты (AITU, NU, ЕНУ), туристы и транзитные пассажиры аэропорта, родители с детьми, пожилые, человек на коляске,
компании 3–8 человек, свидание, деловая встреча, ночь, мороз, «есть 20 минут», «весь день», дела по списку («сначала X, потом Y, в конце домой/в Z»),
концерт/матч/спектакль, «поработать с ноутбуком», «хочу поесть национальное», с собакой. 25%% пишут по-казахски, 15%% по-английски,
остальные по-русски; часть — с опечатками, без запятых, сленгом, как в мессенджере. Время суток — разное (от 06:00 до 01:00).
Места старта и назначения — ТОЛЬКО из этого списка ориентиров Астаны: %s.
Примерно у трети людей место старта не названо — сервис знает его по GPS (тогда укажи gps_at — ориентир из списка, где человек стоит).
Верни ТОЛЬКО JSON: {"personas": [{
 "who": "кто это, 5–10 слов",
 "text": "фраза, как человек её напечатает",
 "now": "HH:MM",
 "gps_at": "ориентир из списка или null",
 "expect": {
   "origin": "ориентир из списка, откуда (или null если gps)",
   "destination": "ориентир из списка, куда в конце, или null если «и обратно»",
   "deadline": "HH:MM или null",
   "via": ["ориентиры по порядку, если человек сам перечислил пункты"] ,
   "needs": ["что обязательно: food | coffee | kids | wheelchair | work | night | sights | walk | event | group | indoor | dog"],
   "language": "ru | kk | en",
   "impossible": true/false — если по времени это физически невыполнимо (например, 10 минут на другой конец города)
 },
 "happy_if": "одна фраза: что человек считает хорошим ответом"}]}"""

JUDGE = """Ты — строгий, но справедливый проверяющий. Человек спросил городской сервис «Окно» в Астане. Оцени, получил ли он то, что хотел.
Тебе дано: кто человек, его фраза, чего он ждёт, и что ответил сервис (план с местами и временем, или отказ с объяснением).
Правила: честный отказ с понятной причиной и советом — это хорошо, если запрос действительно невыполним; плохо — если выполним.
Места должны соответствовать желаниям (поесть → место поесть; ребёнок → подходящее детям; ночь → открыто ночью). Язык ответа — как у человека.
Верни ТОЛЬКО JSON: {"score": 1-5, "problem": "главная проблема одной фразой или пустая строка", "fix": "что поменять в сервисе, одной фразой или пустая строка"}"""


def gen(n):
    out = []
    while len(out) < n:
        k = min(15, n - len(out))
        d = llm._chat(GEN % (k, ", ".join(LANDMARKS)), f"Партия {len(out) // 15 + 1}. Не повторяй типажи из прошлых партий: "
                      + "; ".join(p["who"] for p in out[-30:]), max_tokens=6000, temperature=0.9, budget=90) or {}
        got = [p for p in d.get("personas") or [] if p.get("text") and p.get("now")]
        print("партия:", len(got), flush=True)
        if not got:
            break
        out += got
    for i, p in enumerate(out, 1):
        p["id"] = i
    FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(out)} персонажей → {FILE}")


def lm_point(name):
    p = geo.find_place(name or "")
    return {"lat": p["lat"], "lon": p["lon"], "mine": True} if p else None


def near(a, b, km=0.6):
    return a and b and geo.haversine_km((a["lat"], a["lon"]), (b["lat"], b["lon"])) <= km


def objective(p, r):
    """Проверки кодом: только то, что можно проверить однозначно."""
    e, issues = p.get("expect") or {}, []
    if r.get("error"):
        if not e.get("impossible"):
            issues.append(f"отказ, хотя запрос выполним: {r['error'][:90]}")
        return issues
    if e.get("impossible"):
        issues.append("построил план для невыполнимого запроса")
    stops = [s for s in r.get("steps", []) if s["type"] == "stop"]
    if e.get("deadline") and r.get("summary", {}).get("arrive"):
        dl, arr = e["deadline"], r["summary"]["arrive"]
        if arr > dl and not (dl < "05:00" < arr):
            issues.append(f"опоздание: {arr} > {dl}")
    for s in stops:
        if s["hours"]["status"] != "open":
            issues.append(f"{s['name']}: {s['hours']['text']}")
    want_dest = lm_point(e.get("destination")) if e.get("destination") else None
    if want_dest and not near(r.get("destination"), want_dest, 1.2):
        issues.append(f"конечная точка не та: {r.get('destination', {}).get('label')} вместо {e['destination']}")
    via = [v for v in e.get("via") or [] if v]
    if via:
        got = [s for s in stops]
        pts = [lm_point(v) for v in via]
        order = [next((i for i, s in enumerate(got) if q and near(s, q, 0.8)), None) for q in pts]
        closed_note = any(re.search(r"закрыт|выходной|closed", n, re.I) for n in r.get("notes") or [])
        if None in order and not closed_note:   # честное «сегодня выходной — подобрал другое» не считается провалом
            issues.append(f"не все пункты из списка в маршруте: {via}")
        elif order != sorted(order):
            issues.append("пункты не в том порядке")
    lang = e.get("language")
    txt = " ".join([r.get("headline") or ""] + [s.get("why", "") for s in stops])
    if lang == "kk" and not re.search(r"[әіңғүұқөһ]", txt.lower()):
        issues.append("ответ не на казахском")
    if lang == "en" and re.search(r"[а-яё]{4,}", (r.get("headline") or "").lower()):
        issues.append("ответ не на английском")
    return issues


def summary_for_judge(r):
    if r.get("error"):
        return "ОТКАЗ: " + r["error"] + " " + " ".join(r.get("notes") or [])
    lines = [f"Итог: {r.get('headline')}. Откуда {r['origin']['label']}, куда {r['destination']['label']}, окно {r['now']}–{r['deadline']}, "
             f"на месте в {r['summary']['arrive']}, запас {r['summary']['buffer_min']} мин."]
    for s in r["steps"]:
        if s["type"] == "leg":
            lines.append(f"  дорога: {s['mode']} {s['min']} мин")
        if s["type"] == "stop":
            lines.append(f"  {s['arrive']}–{s['leave']} {s['name']} [{s['category']}] — {s['why']} / {s['todo']} ({s['hours']['text']})")
    lines += [f"  заметка: {n}" for n in (r.get("notes") or [])[:4]]
    return "\n".join(lines)


def run(only=None):
    ps = json.loads(FILE.read_text(encoding="utf-8"))
    rows = []
    for p in ps:
        if only and p["id"] != only:
            continue
        body = {"text": p["text"], "now": p["now"]}
        if p.get("gps_at"):
            body["point"] = lm_point(p["gps_at"])
        t0 = time.time()
        try:
            r = httpx.post(API, json=body, timeout=120).json()
        except Exception as ex:
            r = {"error": f"сервер упал: {ex}"}
        sec = round(time.time() - t0, 1)
        obj = objective(p, r)
        j = llm._chat(JUDGE, json.dumps({"кто": p["who"], "фраза": p["text"], "сейчас": p["now"], "ждёт": p.get("expect"),
                                         "хорошо_если": p.get("happy_if"), "ответ_сервиса": summary_for_judge(r)}, ensure_ascii=False),
                      max_tokens=400, temperature=0.1, budget=30) or {}
        score = int(j.get("score") or 0)
        rows.append({"p": p, "r": r, "obj": obj, "score": score, "problem": j.get("problem", ""), "fix": j.get("fix", ""), "sec": sec})
        print(f"{p['id']:>3} {score}/5 {'OK ' if not obj else 'ERR'} {sec:>5}s  {p['text'][:60]}", flush=True)
        if obj or score <= 3:
            for i in obj:
                print("      ✗", i)
            if j.get("problem"):
                print("      ИИ:", j["problem"], "→", j.get("fix", ""))
    if not only:
        report(rows)


def report(rows):
    n = len(rows)
    good = sum(1 for x in rows if not x["obj"] and x["score"] >= 4)
    avg = sum(x["score"] for x in rows) / max(n, 1)
    L = [f"# «Тайные покупатели»: {n} синтетических жителей и гостей Астаны\n",
         f"Прогон {datetime.now(geo.KZ):%d.%m.%Y %H:%M}. Персонажей придумал ИИ (`scripts/personas.py gen`): студенты, туристы, родители, "
         "пожилые, человек на коляске, компании, ночь, дела по списку; четверть — по-казахски, часть — по-английски, с опечатками. "
         "Каждый спросил «Окно» через тот же сервер, что и сайт. Ответ проверяли **кодом** (успел к сроку, места открыты, та конечная точка, "
         "порядок пунктов, язык) и **отдельным ИИ-судьёй** (оценка 1–5: получил ли человек то, что хотел).\n",
         f"**Итог: {good} из {n} довольны полностью** (нет ошибок по проверкам кода и оценка судьи 4–5), средняя оценка судьи — **{avg:.1f} из 5**.\n",
         "| № | Кто | Фраза | Оценка | Проверки кода | Что не так (судья) |", "|---|---|---|---|---|---|"]
    for x in rows:
        p = x["p"]
        L.append(f"| {p['id']} | {p['who']} | «{p['text']}» ({p['now']}) | {x['score']}/5 | "
                 + ("✅" if not x["obj"] else "❌ " + "; ".join(x["obj"])).replace("|", "/") + f" | {(x['problem'] or '—').replace('|', '/')} |")
    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")
    (ROOT / "docs" / "personas_last.json").write_text(json.dumps(
        [{"id": x["p"]["id"], "score": x["score"], "obj": x["obj"], "problem": x["problem"], "fix": x["fix"]} for x in rows],
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nИТОГ: {good}/{n} довольны, средняя {avg:.2f} → {REPORT}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["gen"]:
        gen(int(sys.argv[2]) if len(sys.argv) > 2 else 60)
    else:
        run(int(sys.argv[2]) if len(sys.argv) > 2 else None)
