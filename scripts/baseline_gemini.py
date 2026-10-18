"""Второй «чатбот» для сравнения: тот же Gemini, что внутри «Окна», но БЕЗ наших данных (без retrieval).
Так видно, что даёт именно RAG: модель одна и та же, разница — только в фактах.
Запуск: python scripts/baseline_gemini.py  →  docs/benchmark_baseline_gemini.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import llm  # noqa: E402

Q = json.loads((ROOT / "docs/benchmark_queries.json").read_text(encoding="utf-8"))
SYSTEM = (f"Ты — обычный ИИ-ассистент в чате. Сегодня {Q['date']}, погода: {Q['weather']}. Пользователь в Астане (Казахстан) просит совет. "
          "Ответь как полезный чат-бот из своих знаний: 1–3 конкретных реальных места в Астане, время прихода и ухода для каждого, "
          "и где и во сколько человек окажется в конце. Старайся, чтобы места были открыты и всё успевалось. "
          'Верни ТОЛЬКО JSON: {"stops": [{"name": "...", "arrive": "HH:MM", "leave": "HH:MM", "why": "..."}], '
          '"end": {"name": "...", "arrive": "HH:MM"}, "deadline": "HH:MM или null"}')
out = []
for q in Q["queries"]:
    d = llm._chat(SYSTEM, f"Сейчас {q['now']}. {q['text']}", max_tokens=900, temperature=0.3, budget=40) or {}
    out.append({"id": q["id"], "stops": d.get("stops") or [], "end": d.get("end") or {}, "deadline": d.get("deadline")})
    print(q["id"], len(out[-1]["stops"]), flush=True)
(ROOT / "docs/benchmark_baseline_gemini.json").write_text(json.dumps(
    {"model_note": f"Gemini ({llm.GEMINI_MAIN}) без данных «Окна» — та же модель, что внутри сервиса", "answers": out},
    ensure_ascii=False, indent=1), encoding="utf-8")
print("ok", len(out))
