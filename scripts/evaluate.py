"""Оценка качества по критериям кейса: 120 синтетических запросов (scripts/synthetic.py) → живой сервер (:8010) →
судья проверяет каждый ответ.

Две части:
1. Честность («groundedness», главное в RAG): каждое утверждение в «почему» и «что делать» сверяется с фактами,
   которые получила генерация (данные места, часы, дорога). Выдумка = утверждение о месте, которого нет в фактах.
   Плюс проверки кодом без ИИ: рейтинг в тексте = рейтинг 2GIS; «рядом / в двух шагах» — только если пешком ≤ 10 мин.
2. Оценки 1–5 по критериям жюри: релевантность, учёт контекста (время, часы, погода, ограничения), качество «почему»,
   конкретность «что делать».

Судья — Gemini с отдельной строгой инструкцией (та же модель, что пишет тексты, — честно отмечено в отчёте).

    .venv/Scripts/python.exe -X utf8 scripts/evaluate.py          # все 120
    .venv/Scripts/python.exe -X utf8 scripts/evaluate.py 5 17     # только эти
Результат: data/dataset/eval_results.jsonl, docs/evaluation.md
"""
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import llm  # noqa: E402

URL = "http://127.0.0.1:8010/api/plan"
QUERIES = ROOT / "data" / "dataset" / "synthetic_queries.jsonl"
OUT = ROOT / "data" / "dataset" / "eval_results.jsonl"
HISTORY = ROOT / "data" / "dataset" / "eval_history.json"   # итоги прошлых прогонов и что исправили после каждого
CRIT = {"relevance": "Релевантность", "context": "Учёт контекста", "why": "Качество «почему»", "todo": "«Что делать»"}

JUDGE = """Ты — строгий эксперт жюри хакатона. Городской сервис «Окно» (Астана) по запросу человека выбрал места алгоритмом,
а ИИ написал к каждому месту «почему» (why) и «что делать» (todo). Тебе даны запрос, время, погода, ответ сервиса и
ФАКТЫ, которые были у ИИ про каждое место (доп_факты, часы, как добраться, время там).

1) Найди ВЫДУМКИ: конкретные утверждения о месте в why/todo, которых НЕТ в фактах и которые нельзя вывести из категории места.
   Выдумка: рейтинг, цена, блюдо/меню, выставка/событие, исторический факт, вид из окна, розетки/Wi-Fi/детская комната,
   «рядом», если добираться на такси или пешком больше 10 мин, точное расстояние/время, не совпадающее с фактами.
   НЕ выдумка: общее предложение действия по типу места («возьми кофе», «пройдись по залам», «сделай фото»),
   общеизвестное о типе места («в ТРЦ можно погреться»), честное «данных нет — уточни на месте».
2) Оцени 1–5:
   relevance — места соответствуют просьбе (поесть → еда, ребёнок → детям, ночь → открыто ночью, язык ответа = язык запроса);
   context — учтены время, окно, часы работы, погода, ограничения (коляска, мороз, вылет); честный отказ при невыполнимом = хорошо;
   why — «почему» конкретное, про ЭТОГО человека, без банальностей и без выдумок;
   todo — «что делать» конкретное действие, реально выполнимое за отведённое время.
   Для отказа (нет мест): оценивай, понятна ли причина и есть ли полезный совет; why/todo оцени по тексту отказа.
Верни ТОЛЬКО JSON: {"relevance":n,"context":n,"why":n,"todo":n,"unsupported":["утверждение — чего нет в фактах"],"problem":"главная проблема одной фразой или пустая строка"}"""


def evidence(q, r):
    if r.get("error"):
        return {"запрос": q["text"], "сейчас": q["now"], "отказ": r["error"], "заметки": (r.get("notes") or [])[:4]}
    reach, prev, stops = {}, None, []
    for st in r["steps"]:
        if st["type"] == "leg":
            prev = st
        elif st["type"] == "stop":
            stops.append({"название": st.get("name_en") or st.get("name_kk") or st["name"], "категория": st["category"],
                          "как_добраться": (f"пешком {prev['min']} мин" if prev and prev["mode"] == "walk" else
                                            f"такси ~{prev['min']} мин" if prev else None),
                          "время_там_мин": st["stay"], "приход": st["arrive"], "часы": st["hours"]["text"],
                          "доп_факты": st.get("facts"), "why": st.get("why"), "todo": st.get("todo")})
    return {"запрос": q["text"], "сейчас": q["now"], "погода": r.get("weather", {}).get("apparent"),
            "симуляция_мороза": q["weather"] == "cold", "заголовок": r.get("headline"),
            "окно_мин": r["summary"]["window_min"], "прибытие": r["summary"]["arrive"],
            "заметки": (r.get("notes") or [])[:4], "места": stops}


def rule_checks(r):
    """Проверки кодом, без ИИ: числа и «рядом» в текстах сверяются с данными."""
    bad = []
    prev = None
    for st in r.get("steps") or []:
        if st["type"] == "leg":
            prev = st
            continue
        if st["type"] != "stop":
            continue
        text = f"{st.get('why', '')} {st.get('todo', '')}"
        rat = (st.get("facts") or {}).get("рейтинг_2гис", "")
        for m in re.finditer(r"(\d[.,]\d)\s*(?:в|in|по|★)?\s*2GIS|рейтинг\w*\s*(\d[.,]\d)|rated\s*(\d[.,]\d)", text, re.I):
            val = next(g for g in m.groups() if g).replace(",", ".")
            if not rat.startswith(val):
                bad.append(f"{st['name']}: рейтинг {val} в тексте, а в 2GIS — {rat or 'нет данных'}")
        near = re.search(r"\bрядом\b|в двух шагах|прямо у|right next|next door|steps away|жанында", text, re.I)
        if near and prev and (prev["mode"] != "walk" or prev["min"] > 10):
            bad.append(f"{st['name']}: «{near.group(0)}», а добираться {prev['mode']} {prev['min']} мин")
    return bad


def run_one(q):
    try:
        r = httpx.post(URL, json={"text": q["text"], "now": q["now"], "weather": q["weather"], "saved": {}}, timeout=180).json()
    except Exception as ex:   # noqa: BLE001
        r = {"error": f"сервер не ответил: {ex}"}
    j = None
    for _ in range(3):
        j = llm._chat(JUDGE, json.dumps(evidence(q, r), ensure_ascii=False), max_tokens=900, temperature=0.1, budget=60)
        if isinstance(j, dict) and all(isinstance(j.get(k), (int, float)) for k in CRIT):
            break
        time.sleep(3)
    j = j if isinstance(j, dict) else {}
    return {"id": q["id"], "family": q["family"], "text": q["text"], "now": q["now"], "refused": bool(r.get("error")),
            "scores": {k: j.get(k) for k in CRIT}, "unsupported": [str(x) for x in (j.get("unsupported") or [])],
            "rule_issues": rule_checks(r), "problem": j.get("problem") or "",
            "answer": r.get("error") or "; ".join(f"{s['name']}: {s.get('why', '')} / {s.get('todo', '')}"
                                                 for s in r.get("steps", []) if s["type"] == "stop")}


def report(rows, label=None):
    judged = [x for x in rows if all(isinstance(x["scores"].get(k), (int, float)) for k in CRIT)]
    avg = {k: sum(x["scores"][k] for x in judged) / max(1, len(judged)) for k in CRIT}
    clean = [x for x in rows if not x["unsupported"] and not x["rule_issues"]]
    with_stops = [x for x in rows if not x["refused"]]
    fam = defaultdict(list)
    for x in judged:
        fam[x["family"]].append(x)
    L = ["# Оценка качества по критериям кейса", "",
         f"{len(rows)} синтетических запросов (`data/dataset/synthetic_queries.jsonl`, 15 типов, 3 языка) прогнаны через живую систему; "
         "каждый ответ проверен судьёй-ИИ со строгой отдельной инструкцией и проверками кодом. Скрипт — `scripts/evaluate.py`, "
         "все оценки и замечания — `data/dataset/eval_results.jsonl`.", "",
         "## Честность объяснений (без выдумок)", "",
         f"- **{len(clean)} из {len(rows)}** ответов — ни одного утверждения о месте, которого нет в данных "
         f"({len(clean) / max(1, len(rows)):.0%}).",
         f"- Проверки кодом (рейтинг в тексте = рейтинг 2GIS; «рядом» — только если пешком ≤ 10 мин): "
         f"замечаний — {sum(len(x['rule_issues']) for x in rows)}.",
         f"- Ответов с местами: {len(with_stops)}, честных отказов с советом: {len(rows) - len(with_stops)}.", "",
         *history_table(len(clean), len(rows), avg, label),
         "## Оценки 1–5 по критериям жюри", "",
         "| Критерий | Средняя оценка |", "|---|---|",
         *[f"| {CRIT[k]} | **{avg[k]:.2f}** |" for k in CRIT], "",
         "## По типам запросов", "",
         "| Тип запроса | N | " + " | ".join(CRIT.values()) + " | Без выдумок |", "|---|---|" + "---|" * (len(CRIT) + 1),
         *[f"| {f} | {len(xs)} | " + " | ".join(f"{sum(x['scores'][k] for x in xs) / len(xs):.1f}" for k in CRIT) +
           f" | {sum(1 for x in xs if not x['unsupported'] and not x['rule_issues'])}/{len(xs)} |" for f, xs in fam.items()], ""]
    issues = [x for x in rows if x["unsupported"] or x["rule_issues"]]
    if issues:
        L += ["## Найденные выдумки и неточности (честно, как есть)", ""]
        for x in issues:
            L.append(f"- №{x['id']} «{x['text']}» — " + "; ".join(x["rule_issues"] + x["unsupported"]))
        L.append("")
    low = sorted([x for x in judged if min(x["scores"].values()) <= 3], key=lambda x: sum(x["scores"].values()))[:10]
    if low:
        L += ["## Самые слабые ответы по мнению судьи", ""]
        for x in low:
            L.append(f"- №{x['id']} «{x['text']}» ({' / '.join(str(x['scores'][k]) for k in CRIT)}) — {x['problem']}")
        L.append("")
    L += ["## Ограничения оценки", "",
          "- Судья — та же модель Gemini, что пишет тексты: возможна снисходительность к себе. Поэтому числа и «рядом» "
          "дополнительно проверяются кодом, а все замечания судьи выложены целиком.",
          "- Запросы синтетические (сгенерированы шаблонами с опечатками и сленгом), а не от реальных пользователей.", ""]
    (ROOT / "docs" / "evaluation.md").write_text("\n".join(L), encoding="utf-8")
    print(f"без выдумок {len(clean)}/{len(rows)}; " + ", ".join(f"{k} {v:.2f}" for k, v in avg.items()))


def history_table(clean, n, avg, label):
    """Было → стало: каждый прогон оценки и что исправили по его замечаниям."""
    hist = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    if label:
        old = next((h for h in hist if h["label"] == label), {})
        hist = [h for h in hist if h["label"] != label] + [{**old, "label": label, "n": n, "clean": clean,
                                                            "avg": {k: round(v, 2) for k, v in avg.items()}}]
        HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    if len(hist) < 2:
        return []
    out = ["## Было → стало", "", "Судья нашёл слабые места — исправили — прогнали те же 120 запросов снова.", "",
           "| Прогон | Без выдумок | " + " | ".join(CRIT.values()) + " |", "|---|---|" + "---|" * len(CRIT)]
    for h in hist:
        out.append(f"| {h['label']} | {h['clean']}/{h['n']} | " + " | ".join(f"{h['avg'][k]:.2f}" for k in CRIT) + " |")
    out.append("")
    for h in hist:
        if h.get("fixed_after"):
            out.append(f"- После «{h['label']}» исправлено: {h['fixed_after']}.")
    return out + [""]


def main():
    if sys.argv[1:2] == ["report"]:   # пересобрать отчёт из сохранённых оценок, не гоняя запросы заново
        rows = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
        report(rows, label=" ".join(sys.argv[2:]) or None)
        return
    pick = {int(a) for a in sys.argv[1:]}
    qs = [json.loads(x) for x in QUERIES.read_text(encoding="utf-8").splitlines() if x.strip()]
    qs = [q for q in qs if not pick or q["id"] in pick]
    rows = []
    with ThreadPoolExecutor(3) as ex:
        for row in ex.map(run_one, qs):
            rows.append(row)
            s = row["scores"]
            flag = "OK " if not row["unsupported"] and not row["rule_issues"] else "!! "
            print(f"{row['id']:3} {flag} {s.get('relevance')}/{s.get('context')}/{s.get('why')}/{s.get('todo')}  {row['text'][:60]}", flush=True)
            for b in row["rule_issues"] + row["unsupported"]:
                print("      ✗", b[:160])
    if not pick:
        OUT.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8")
        report(rows, label=time.strftime("после исправлений, %d.%m %H:%M"))


if __name__ == "__main__":
    main()
