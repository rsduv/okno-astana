"""Известные места Астаны из Wikidata + короткое описание из Википедии: python scripts/collect_wiki.py

Зачем: в OSM у достопримечательностей почти нет описаний. Wikidata даёт честный сигнал известности
(на скольких языках есть статья) и ссылку на статью; из Википедии берём первые предложения — это факты
для «почему», которые ИИ может пересказать со ссылкой на источник. Лицензия текстов — CC BY-SA, указываем.
Результат: data/wiki.json (публикуется вместе с репозиторием).
"""
import json
import urllib.parse
import re
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "wiki.json"
UA = {"User-Agent": "OknoAstana/0.1 (https://github.com/rsduv/okno-astana)"}  # формат по правилам Wikimedia: Имя/версия (контакт)
QUERY = """
SELECT ?item ?itemLabel ?coord ?sl ?article (GROUP_CONCAT(DISTINCT ?tl; separator="|") AS ?types) WHERE {
  ?item wdt:P131 wd:Q1520; wdt:P625 ?coord; wikibase:sitelinks ?sl.
  OPTIONAL { ?item wdt:P31 ?t. ?t rdfs:label ?tl FILTER(lang(?tl)="ru") }
  OPTIONAL { ?article schema:about ?item; schema:isPartOf <https://ru.wikipedia.org/> }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ru,kk,en". ?item rdfs:label ?itemLabel. }
} GROUP BY ?item ?itemLabel ?coord ?sl ?article
"""
# тип из Wikidata -> наша категория; всё остальное (вузы, районы, улицы, министерства, события) пропускаем
TYPES = [("мечеть", "мечеть"), ("собор", "собор"), ("синагог", "собор"), ("церк", "собор"), ("храм", "собор"),
         ("музей", "музей"), ("театр", "театр"), ("концертн", "театр"), ("цирк", "театр"), ("библиотек", "библиотека"),
         ("парк", "парк"), ("памятник", "памятник"), ("монумент", "памятник"), ("башн", "смотровая"),
         ("площадь", "достопримечательность"), ("мост", "достопримечательность"), ("дворец", "достопримечательность"),
         ("шато", "достопримечательность"), ("резиденц", "достопримечательность"), ("национальный символ", "достопримечательность"),
         ("торговый центр", "торговый центр"), ("небоскрёб", "достопримечательность")]


def category(types: str, label: str) -> str | None:
    t = types.lower()
    if re.search(r"университет|район|улиц|проспект|министерств|предприят|школ|гостиниц|вики|чемпионат|выставка|станция|аэропорт|парламент", t + " " + label.lower()):
        return None
    for key, cat in TYPES:
        if key in t:
            return cat
    return None


IMAGES: dict[str, str] = {}   # статья → миниатюра фото (Wikimedia Commons, через Википедию)


def summary(article_url: str) -> str | None:
    title = article_url.rsplit("/", 1)[-1]
    try:
        title = urllib.parse.unquote(title)  # httpx сам кодирует URL — иначе «%» кодируется дважды
        r = httpx.get(f"https://ru.wikipedia.org/api/rest_v1/page/summary/{title}", headers=UA, timeout=15)
        j = r.json()
        txt = j.get("extract", "")
        th = (j.get("thumbnail") or {}).get("source")
        if th:
            IMAGES[article_url] = th
    except Exception:
        return None
    txt = re.sub(r"\s*\([^)]*\)", "", txt)  # убираем скобки с транслитерацией и датами
    sents = re.split(r"(?<=[.!?])\s+", txt)
    out = ""
    for s in sents:
        if len(out) + len(s) > 320:
            break
        out += (" " if out else "") + s
    return out or None


def add_images(out: dict) -> None:
    """Фото мест: главное изображение из Wikidata (P18) → миниатюра с Wikimedia Commons (свободные лицензии)."""
    ids = [q for q, w in out.items() if not w.get("image")]
    for i in range(0, len(ids), 50):
        try:
            j = httpx.get("https://www.wikidata.org/w/api.php", headers=UA, timeout=30, params={
                "action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "props": "claims", "format": "json"}).json()
        except Exception:
            continue
        for q, e in (j.get("entities") or {}).items():
            c = (e.get("claims") or {}).get("P18")
            if c and q in out:
                fn = c[0]["mainsnak"]["datavalue"]["value"].replace(" ", "_")
                out[q]["image"] = f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(fn)}?width=480"


def add_labels(out: dict) -> None:
    """Названия на английском и казахском из Wikidata — для туристов и ответа на языке запроса."""
    ids = list(out)
    for i in range(0, len(ids), 50):
        try:
            j = httpx.get("https://www.wikidata.org/w/api.php", headers=UA, timeout=30, params={
                "action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "props": "labels", "languages": "en|kk", "format": "json"}).json()
        except Exception:
            continue
        for q, e in (j.get("entities") or {}).items():
            for lang in ("en", "kk"):
                v = ((e.get("labels") or {}).get(lang) or {}).get("value")
                if v and q in out:
                    out[q][f"name_{lang}"] = v


def osm_images() -> None:
    """Фото для мест из OpenStreetMap с тегом wikidata (Хан Шатыр, музеи, театры…) → data/images.json."""
    poi = json.loads((OUT.parent / "poi.json").read_text(encoding="utf-8"))
    ids = sorted({p["tags"]["wikidata"] for p in poi if (p.get("tags") or {}).get("wikidata", "").startswith("Q")})
    imgs = {}
    for i in range(0, len(ids), 50):
        try:
            j = httpx.get("https://www.wikidata.org/w/api.php", headers=UA, timeout=30, params={
                "action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "props": "claims", "format": "json"}).json()
        except Exception:
            continue
        for q, e in (j.get("entities") or {}).items():
            c = (e.get("claims") or {}).get("P18")
            if c:
                fn = c[0]["mainsnak"]["datavalue"]["value"].replace(" ", "_")
                imgs[q] = f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(fn)}?width=480"
    (OUT.parent / "images.json").write_text(json.dumps(imgs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"фото для мест OSM: {len(imgs)}")


def main():
    r = httpx.get("https://query.wikidata.org/sparql", params={"query": QUERY, "format": "json"}, headers=UA, timeout=60)
    rows = r.json()["results"]["bindings"]
    out = {}
    for b in rows:
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        label = b["itemLabel"]["value"]
        cat = category(b.get("types", {}).get("value", ""), label)
        if not cat or qid in out:
            continue
        lon, lat = map(float, re.findall(r"-?\d+\.?\d*", b["coord"]["value"])[:2])
        art = b.get("article", {}).get("value")
        out[qid] = {"qid": qid, "name": label, "category": cat, "lat": lat, "lon": lon, "sitelinks": int(b["sl"]["value"]),
                    "article": art, "extract": summary(art) if art else None}
        if art and art in IMAGES:
            out[qid]["image"] = IMAGES[art]
        print(f"{out[qid]['sitelinks']:3} {label} [{cat}] {'+текст' if out[qid]['extract'] else ''}")
        time.sleep(0.3)
    add_images(out)
    add_labels(out)
    osm_images()
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"готово: {len(out)} мест → {OUT}")


if __name__ == "__main__":
    main()
