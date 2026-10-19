"""Часы работы: разбор формата OpenStreetMap (opening_hours) и честная оценка уверенности.

Главный вопрос не «открыто ли сейчас», а «будет ли открыто всё время, пока человек там»:
interval_status(tags, category, arrive, leave) -> (status, confidence, text)
  status:     "open" | "closed" | "closes_soon" | "unknown"
  confidence: "verified" (часы из OSM) | "estimate" (типичные для категории) | "outdoor" (открытое место)
"""
from __future__ import annotations

import re
from functools import lru_cache
from datetime import datetime, timedelta

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

# Типичные часы по категории — используются ТОЛЬКО как помеченная оценка, когда в OSM данных нет
TYPICAL = {
    "кафе": "08:00-22:00", "ресторан": "11:00-23:00", "фастфуд": "09:00-23:00", "бар": "16:00-02:00",
    "ночной клуб": "22:00-05:00", "торговый центр": "10:00-22:00", "кино": "10:00-01:00", "детский центр": "10:00-21:00",
    "театр": "10:00-20:00", "музей": "10:00-18:00", "галерея": "10:00-19:00", "арт-центр": "10:00-19:00",
    "библиотека": "09:00-19:00", "книжный": "10:00-20:00", "коворкинг": "09:00-21:00",
    "мечеть": "06:00-21:00", "собор": "08:00-19:00", "океанариум": "10:00-20:00", "зоопарк": "10:00-19:00",
    "развлечения": "10:00-22:00", "квест": "10:00-23:00", "боулинг": "12:00-02:00", "каток": "10:00-22:00",
    "батутный парк": "10:00-21:00", "парк развлечений": "10:00-22:00", "аквапарк": "10:00-21:00",
}
# Открытые места: часы не нужны, но ночью и в мороз — свои ограничения (их учитывает планировщик)
OUTDOOR = {"парк", "сад", "памятник", "фонтан", "смотровая", "детская площадка", "достопримечательность"}

_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\+?")


def _day_set(sel: str) -> set[int] | None:
    """'Mo-Fr,Su' -> {0,1,2,3,4,6}. None если не похоже на дни."""
    out: set[int] = set()
    for part in sel.split(","):
        part = part.strip()
        if part in ("PH", "SH"):
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            k = i
            while True:  # поддержка «Su-Sa» с переходом через неделю
                out.add(k)
                if k == j:
                    break
                k = (k + 1) % 7
        elif part in DAYS:
            out.add(DAYS.index(part))
        else:
            return None
    return out


@lru_cache(maxsize=4096)
def parse(oh: str) -> dict[int, list[tuple[int, int]]] | None:
    """Строка OSM -> {день недели: [(начало_мин, конец_мин)]}, конец может быть > 1440 (после полуночи).
    None — если формат не понят (тогда честно говорим «оценка»)."""
    if not oh:
        return None
    oh = oh.strip()
    # частая ошибка разметки: правила через запятую «Mo-Fr 08:00-24:00, Sa-Su 09:00-24:00»
    oh = re.sub(r"(?<=\d),\s*(?=[A-Z][a-z])", "; ", oh)
    oh = re.sub(r"(?<=[A-Za-z]),\s+(?=[A-Z][a-z])", ",", oh)  # «Su, Tu-Fr» -> «Su,Tu-Fr»
    oh = re.sub(r"(\d{1,2}:\d{2})\+", r"\1-24:00", oh)  # «08:00+» = до конца дня (открытый конец)
    if oh == "24/7":
        return {d: [(0, 1440)] for d in range(7)}
    week: dict[int, list[tuple[int, int]]] = {}
    for rule in oh.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        m = re.match(r"^([A-Za-z,\- ]+?)\s+(.*)$", rule)
        if m and _day_set(m.group(1).replace(" ", "")) is not None:
            days, rest = _day_set(m.group(1).replace(" ", "")), m.group(2).strip()
        elif _TIME.match(rule) or rule in ("off", "closed"):
            days, rest = set(range(7)), rule
        else:
            # «Mo-Su off», «PH off» и прочее — пытаемся понять, иначе сдаёмся
            m2 = re.match(r"^([A-Za-z,\-]+)\s*(off|closed)$", rule)
            if m2 and _day_set(m2.group(1)) is not None:
                for d in _day_set(m2.group(1)):
                    week[d] = []
                continue
            if re.match(r"^PH\b", rule):
                continue
            return None
        if rest in ("off", "closed"):
            for d in days:
                week[d] = []
            continue
        spans = []
        for t in rest.split(","):
            mt = _TIME.fullmatch(t.strip())
            if not mt:
                return None
            h1, m1, h2, m2_ = map(int, mt.groups())
            s, e = h1 * 60 + m1, h2 * 60 + m2_
            if e <= s:
                e += 1440  # работает после полуночи
            spans.append((s, e))
        for d in days:
            week[d] = spans  # более поздние правила перекрывают ранние — как в OSM
    return week if week else None


def _abs_intervals(week, day0: datetime):
    """Абсолютные интервалы работы вокруг даты: вчера, сегодня, завтра."""
    out = []
    for off in (-1, 0, 1):
        d = (day0 + timedelta(days=off)).replace(hour=0, minute=0, second=0, microsecond=0)
        for s, e in week.get(d.weekday(), []):
            out.append((d + timedelta(minutes=s), d + timedelta(minutes=e)))
    out.sort()
    merged = []
    for s, e in out:  # склеиваем «до 24:00» + «с 00:00»
        if merged and s <= merged[-1][1] + timedelta(minutes=1):  # «до 23:59» + «с 00:00» — без разрыва
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _fmt(week, day: datetime) -> str:
    spans = week.get(day.weekday())
    if spans is None:
        return ""
    if not spans:
        return "сегодня выходной"
    if len(spans) == 1 and spans[0][0] == 0 and spans[0][1] >= 1439:
        return "круглосуточно"
    return ", ".join(f"{s // 60 % 24:02d}:{s % 60:02d}–{e // 60 % 24:02d}:{e % 60:02d}" for s, e in spans)


def next_open(tags: dict, category: str, t: datetime, within_min: int = 240):
    """Когда место откроется, если сейчас закрыто, но откроется скоро (Мега в 9:20 → 10:00). Иначе None."""
    raw = (tags or {}).get("opening_hours", "")
    week = parse(raw) if raw else None
    if week is None:
        typical = TYPICAL.get(category)
        week = parse(typical) if typical else None
    if not week:
        return None
    for s, e in _abs_intervals(week, t):
        if t < s <= t + timedelta(minutes=within_min):
            return s
    return None


def interval_status(tags: dict, category: str, arrive: datetime, leave: datetime):
    """Открыто ли место на весь визит [arrive, leave]. Результат запоминаем: при переборе маршрутов
    одни и те же «место + время» проверяются тысячи раз."""
    return _status((tags or {}).get("opening_hours", ""), category, arrive, leave)


@lru_cache(maxsize=200_000)
def _status(raw: str, category: str, arrive: datetime, leave: datetime):
    if category in OUTDOOR and not raw:
        return "open", "outdoor", "открытое место — часы не нужны"
    if raw.strip().lower() == "closed":
        return "closed", "verified", "в OSM помечено как закрытое"
    week = parse(raw)
    confidence = "verified"
    if week is None:
        typical = TYPICAL.get(category)
        if not typical:
            return "unknown", "estimate", "часов работы нет в данных"
        week = parse(typical)
        confidence = "estimate"
    for s, e in _abs_intervals(week, arrive):
        if s <= arrive < e:
            hours = _fmt(week, arrive) or f"до {e:%H:%M}"
            if leave <= e:
                label = f"{hours} · открыто, пока ты там" if confidence == "verified" else f"обычно {hours} · оценка, данных нет"
                return "open", confidence, label
            mins = int((e - arrive).total_seconds() // 60)
            return "closes_soon", confidence, f"закрывается в {e:%H:%M} — у тебя будет {mins} мин"
    hours = _fmt(week, arrive)
    label = f"{hours} · к твоему приходу закрыто" if confidence == "verified" else f"обычно {hours} · скорее всего закрыто"
    return "closed", confidence, label
