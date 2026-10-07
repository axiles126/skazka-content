#!/usr/bin/env python3
"""library.json -> готовые сказки в catalog.json (текст + озвучка 4 голосами).

Для каждой записи {id, lang, title, url}:
  1. качает страницу (браузерный UA) и вытаскивает текст trafilatura;
  2. Gemini готовит полный текст сказки с режиссёрскими метками (возраст 4–5, тон спокойный);
     результат кладётся в drafts/<lang>-<id>.json — повторно за текст не платим;
  3. озвучивает голосами Achernar, Aoede, Charon, Orus (длинную — кусками, склеивает в один mp3);
  4. mp3 -> релиз "audio", запись -> catalog.json (сразу после каждого голоса).

Уже готовое пропускается по хешу. Квота/биллинг кончились -> сохраняем сделанное и выходим (код 0).

Env: GCP_TTS_KEY (обязательно), LIMIT (сколько сказок обработать за прогон, 0 = все),
     ONLY (id через запятую), TEXT_MODEL, TTS_MODEL, DRY_RUN=1 (без загрузки в релиз).
"""
import base64
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import trafilatura

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "library.json"
CATALOG = ROOT / "catalog.json"
DRAFTS = ROOT / "drafts"
OUT = ROOT / "out"

REPO = os.environ.get("GITHUB_REPOSITORY", "axiles126/skazka-content")
RELEASE = "audio"
BASE_URL = f"https://github.com/{REPO}/releases/download/{RELEASE}"

KEY = os.environ.get("GCP_TTS_KEY", "").strip()
TEXT_MODEL = os.environ.get("TEXT_MODEL", "").strip() or "gemini-3.8-flash"
TTS_MODEL = os.environ.get("TTS_MODEL", "").strip() or "gemini-3.1-flash-tts-preview"
ALL_VOICES = [("Achernar", "FEMALE"), ("Aoede", "FEMALE"), ("Charon", "MALE"), ("Orus", "MALE")]
# По умолчанию заранее НЕ озвучиваем ничего — только готовим текст (копейки). Голос
# озвучивается, когда пользователь впервые включает сказку: приложение заказывает его у
# Cloud Function generate-story (режим voice_for), дальше всем из кеша.
# VOICES=default — заранее только Achernar, VOICES=all — все 4.
_V = os.environ.get("VOICES", "").strip()
VOICES = ALL_VOICES if _V == "all" else ALL_VOICES[:1] if _V == "default" else []
LIMIT = int(os.environ.get("LIMIT", "") or "0")
ONLY = {s.strip() for s in os.environ.get("ONLY", "").split(",") if s.strip()}
DRY = os.environ.get("DRY_RUN") == "1"
CHECK = os.environ.get("CHECK") == "1"   # только проверить источники, без Gemini
CHUNK_CHARS = 4000
SOURCE_LIMIT = 30000
PROMPT_VERSION = 3          # 3: сказки целиком — части по 2500 симв., проверка длины, повтор при сокращении
# Запрет отсебятины в конце добавлен без смены версии: уже готовые сказки не переделываем
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
LOCALE_NAMES = {"uk": "украинском", "ru": "русском"}
META = {"uk": "українська народна казка · {} хв", "ru": "русская народная сказка · {} мин"}
# origin — откуда сказка, если язык другой (украинская сказка на русском и т. п.)
META_FOREIGN = {("ru", "uk"): "украинская народная сказка · {} мин",
                ("uk", "ru"): "російська народна казка · {} хв"}


def meta(item: dict, minutes: int) -> str:
    o = item.get("origin") or item["lang"]
    return META_FOREIGN.get((item["lang"], o), META[item["lang"]]).format(minutes)
TAG = re.compile(r"\[[^\]]*\]\s*")


class Stop(Exception):
    """Квота / деньги кончились — дальше не идём."""


def sha(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:12]


def plain(t: str) -> str:
    return TAG.sub("", t).strip()


def api(path: str, body: dict, tries: int = 4) -> dict:
    req_body = json.dumps(body).encode()
    for attempt in range(tries):
        req = urllib.request.Request(f"{GEMINI_API}/{path}", data=req_body, method="POST", headers={
            "Content-Type": "application/json; charset=utf-8", "x-goog-api-key": KEY})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:600].replace("\n", " ")
            low = msg.lower()
            if e.code == 402 or "billing" in low or "credits" in low or "per day" in low:
                raise Stop(f"{e.code}: {msg}")
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = 20 * (attempt + 1)
                print(f"  Google {e.code}, жду {wait} с…")
                time.sleep(wait)
                continue
            if e.code == 429:
                raise Stop(f"429: {msg}")
            raise RuntimeError(f"Google {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                time.sleep(10)
                continue
            raise RuntimeError(f"сеть: {e}")
    raise RuntimeError("нет ответа")


# ---------- источник ----------

VARIANT = re.compile(r"^\d{1,3}(\[\d+\])?(\s|$)")
NOTES = re.compile(r"^(Примечания|Комментарии|Варианты|Источник)\b")


def first_variant(text: str) -> str:
    """Викитека (Афанасьев): на странице все варианты сказки подряд, каждый начинается
    с номера («95[1] Жили-были…», «96 …»). Берём первый, без сносок и примечаний."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines or not VARIANT.match(lines[0]):
        return text
    out = [VARIANT.sub("", lines[0], count=1)]
    for ln in lines[1:]:
        if NOTES.match(ln) or (VARIANT.match(ln) and sum(map(len, out)) > 300):
            break
        out.append(ln)
    return re.sub(r"\[\d+\]", "", "\n".join(out)).strip()


def fetch_source(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "uk,ru,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        html = trafilatura.fetch_url(url)
    if not html:
        raise RuntimeError("страница не открылась")
    text = (trafilatura.extract(html, favor_recall=True, include_comments=False,
                                include_tables=False) or "").strip()
    if len(text) < 200:
        raise RuntimeError(f"на странице мало текста ({len(text)} симв.)")
    if "wikisource.org" in url:
        text = first_variant(text)
    if len(text) > SOURCE_LIMIT:
        print(f"::warning::{url}: текст {len(text)} симв., обрезан до {SOURCE_LIMIT}")
    return text[:SOURCE_LIMIT]


# ---------- текст ----------

PART_CHARS = 2500
PART_NOTE = """

ВНИМАНИЕ: выше не вся сказка «{title}», а только её часть {k} из {n} (сказка длинная и
разрезана по абзацам). Подготовь ТОЛЬКО эту часть — полностью, каждое событие и каждую
реплику, без сокращений. Не начинай сказку заново, если это не первая часть, и не
заканчивай её, если это не последняя: последний абзац — ровно там, где кончается эта часть.
Поля title и scene заполни как обычно."""


MIN_RATIO = 0.8     # часть короче 80% исходника — считаем сокращённой и повторяем
SHORT_NOTE = """

ВНИМАНИЕ: прошлый вариант получился сокращённым ({got} символов при {need} в исходнике).
Нужен ВЕСЬ текст этой части — каждое предложение исходника, без пропусков."""


def split_source(text: str) -> list:
    if len(text) <= PART_CHARS + 700:
        return [text]
    out, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + len(ln) > PART_CHARS:
            out.append(cur); cur = ""
        cur += ln + "\n"
    if cur.strip():
        if out and len(cur) < 700:
            out[-1] += cur
        else:
            out.append(cur)
    return out

def prompt_full(source: str, lang: str) -> str:
    # = story_prompt_full из Note/tools/cloud_function/generate_story/main.py, age=4, tone=calm
    ln = LOCALE_NAMES.get(lang, "украинском")
    return f"""Вот полный текст народной сказки, скачанный со страницы сайта (в нём могут
быть остатки меню, рекламы или другого мусора сайта — их нужно отбросить):
---
{source}
---

Подготовь из этого ТОЧНО ТОТ ЖЕ текст сказки на {ln} языке
для чтения вслух перед сном ребёнку 4 лет. Это не пересказ и не новая история по
мотивам — используй весь сюжет, всех персонажей, все события и ту же концовку, что в
исходном тексте, ничего не добавляя от себя и ничего не выдумывая.

Разрешено и нужно сделать только это:
1. Убрать мусор сайта (меню, рекламу, подписи, не относящиеся к сказке фразы).
2. Если язык текста не совпадает с {ln} — аккуратно
   перевести, сохранив стиль и все детали, а не пересказать своими словами.
   Если текст в старой орфографии (ѣ, і, ъ на конце слов) или с диалектными словами
   (это может быть фольклорная запись XIX века) — привести к современной орфографии,
   а непонятные ребёнку старинные и диалектные слова заменить понятными, не меняя событий.
   Сноски, номера вариантов и примечания собирателя — отбросить.
3. Текст ЦЕЛИКОМ: каждое предложение исходника должно остаться — каждое событие, каждая
   деталь, каждая реплика и каждый повтор (повторы в сказках важны). Не сокращай, не
   пересказывай короче, не объединяй и не выбрасывай предложения. Разрешено только заменить
   непонятное ребёнку слово понятным. Длина результата — примерно как у самой сказки в исходнике.
4. Разбить на естественные абзацы для чтения вслух.
5. Перед репликами и важными словами поставить режиссёрские метки на английском в
   квадратных скобках для озвучки — например [warmly], [sighs], [amazed], [whispering].
   Общая подача: спокойная, мягкая, убаюкивающая подача. Метки не читаются вслух, это
   подсказка для актёра. Текст самой сказки не меняется этими метками — только размечается.

Запрещено: добавлять в начало или в конец что-либо от себя — пожелания спокойной ночи,
обращения к слушателю («спи, малыш», «и ты засыпай»), мораль, выводы, «вот и сказке
конец». Последний абзац — это последние события и слова исходной сказки, и только они.

Ответь СТРОГО в формате JSON без пояснений и без markdown-обёртки:
{{"title": "...", "meta": "народна казка · N мин", "scene": "...", "paragraphs": ["...", "..."]}}"""


def text_hash(item: dict) -> str:
    """rev в library.json — пересоздать текст одной сказки (старый остаётся, пока новый не готов)."""
    parts = [item["url"], item["lang"], TEXT_MODEL, PROMPT_VERSION]
    if item.get("rev"):
        parts.append(item["rev"])
    return sha(*parts)


def make_text(item: dict) -> dict:
    """Черновик текста из drafts/ или новый через Gemini."""
    h = text_hash(item)
    path = DRAFTS / f"{item['lang']}-{item['id']}.json"
    if path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("hash") == h:
            return d
    source = fetch_source(item["url"])
    # Длинную сказку Gemini за один раз сокращает (15 тыс. -> 6) — режем на части
    # по абзацам и готовим каждую отдельно, потом склеиваем.
    parts = split_source(source)
    s, paras = {}, []
    short = 0
    for k, part in enumerate(parts, 1):
        prompt = prompt_full(part, item["lang"])
        if len(parts) > 1:
            prompt += PART_NOTE.format(k=k, n=len(parts), title=item["title"])
        best, best_len = None, -1
        for attempt in range(2):
            extra = "" if attempt == 0 else SHORT_NOTE.format(got=best_len, need=len(part))
            r = api(f"models/{TEXT_MODEL}:generateContent", {
                "contents": [{"parts": [{"text": prompt + extra}]}],
                "generationConfig": {"responseMimeType": "application/json"}})
            got = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
            n = sum(len(plain(p)) for p in got.get("paragraphs", []))
            if n > best_len:
                best, best_len = got, n
            if n >= len(part) * MIN_RATIO:
                break
            print(f"  часть {k}: {n} из {len(part)} симв. — похоже на сокращение, повтор")
        if best_len < len(part) * MIN_RATIO:
            short += 1
        if k == 1:
            s = best
        paras += [p.strip() for p in best.get("paragraphs", []) if plain(p)]
    print(f"  частей: {len(parts)}, {sum(len(plain(p)) for p in paras)} из {len(source)} симв.")
    if short:
        print(f"::warning::{item['id']} ({item['lang']}): {short} из {len(parts)} частей короче исходника и после повтора — проверь")
    if not paras:
        raise RuntimeError("Gemini вернул пустой текст")
    out_len = sum(len(plain(p)) for p in paras)
    if out_len < len(source) * 0.3:
        print(f"::warning::{item['id']}: текст короче источника ({out_len} из {len(source)}) — проверь")
    d = {"hash": h, "id": item["id"], "lang": item["lang"], "title": item["title"],
         "scene": s.get("scene", ""), "paragraphs": paras, "src": item["url"]}
    DRAFTS.mkdir(exist_ok=True)
    path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


# ---------- озвучка ----------

def director_prompt(scene: str, text: str) -> str:
    return (
        "# AUDIO PROFILE: A loving parent reading a bedtime story aloud\n"
        f"## THE SCENE: {scene or 'A cozy dim bedroom at night, a parent tells a story to a small child.'}\n"
        "### DIRECTOR'S NOTES\n"
        "Style: slowly, warm, calm and gentle, like a parent telling a bedtime story to a "
        "small child. Expressive: wonder, effort, disappointment, joy — but always soft.\n"
        "Pacing: unhurried, natural pauses between paragraphs; repetitions get a playful rhythm.\n"
        "Words in square brackets are performance directions — never read them aloud.\n"
        "#### TRANSCRIPT\n" + text)


def find_audio(obj):
    if isinstance(obj, dict):
        data = obj.get("data")
        mime = str(obj.get("mime_type") or obj.get("mimeType") or "")
        if isinstance(data, str) and (mime.startswith("audio") or obj.get("type") == "audio"):
            return base64.b64decode(data)
        for v in obj.values():
            if (a := find_audio(v)) is not None:
                return a
    elif isinstance(obj, list):
        for v in obj:
            if (a := find_audio(v)) is not None:
                return a
    return None


def synth(text: str, scene: str, voice: str) -> bytes:
    """Один кусок -> сырой PCM 16 бит / 24 кГц / моно. Пустой ответ — повторяем."""
    for attempt in range(3):
        r = api(f"models/{TTS_MODEL}:generateContent", {
            "contents": [{"parts": [{"text": director_prompt(scene, text)}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}})
        pcm = find_audio(r)
        if pcm:
            if pcm[:4] == b"RIFF":
                pcm = pcm[44:]
            return pcm
        print(f"  {voice}: пустой ответ, повтор {attempt + 1}")
        time.sleep(5)
    raise RuntimeError(f"{voice}: TTS не вернул аудио")


def chunks(paras: list) -> list:
    out, cur, n = [], [], 0
    for p in paras:
        if cur and n + len(p) > CHUNK_CHARS:
            out.append(cur); cur, n = [], 0
        cur.append(p); n += len(p)
    if cur:
        out.append(cur)
    return out


def to_mp3(pcm: bytes, dst: pathlib.Path) -> float:
    with tempfile.NamedTemporaryFile(suffix=".pcm") as f:
        f.write(pcm); f.flush()
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
                        "-i", f.name, "-af",
                        "silenceremove=stop_periods=-1:stop_duration=1.4:stop_threshold=-45dB:stop_silence=1.1",
                        "-ac", "1", "-codec:a", "libmp3lame", "-q:a", "4", str(dst)], check=True)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(dst)], capture_output=True, text=True).stdout.strip()
    return float(dur or 0)


def upload(f: pathlib.Path) -> None:
    if DRY:
        print(f"  (dry) {f.name}")
        return
    subprocess.run(["gh", "release", "upload", RELEASE, str(f), "--clobber", "-R", REPO], check=True)


# ---------- каталог ----------

def load_catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def save_catalog(cat: dict) -> None:
    CATALOG.write_text(json.dumps(cat, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def is_done(cat: dict, item: dict) -> bool:
    d = DRAFTS / f"{item['lang']}-{item['id']}.json"
    if not d.exists():
        return False
    draft = json.loads(d.read_text(encoding="utf-8"))
    if draft.get("hash") != text_hash(item):
        return False
    st = next((s for s in cat["stories"] if s["id"] == item["id"] and s["lang"] == item["lang"]), None)
    if not st:
        return False
    have = {v.get("hash") for v in st.get("voices", [])}
    return all(voice_hash(draft, v) in have for v, _ in VOICES)


def voice_hash(draft: dict, voice: str) -> str:
    return sha(draft["paragraphs"], draft.get("scene", ""), TTS_MODEL, voice, PROMPT_VERSION)


def process(cat: dict, item: dict) -> None:
    lang, sid = item["lang"], item["id"]
    draft = make_text(item)
    paras = draft["paragraphs"]
    weights = [max(1, len(plain(p))) for p in paras]
    st = next((s for s in cat["stories"] if s["id"] == sid and s["lang"] == lang), None)
    if st and st.get("hash") != draft["hash"]:  # текст пересоздан — обновляем, старая озвучка не подходит
        words = sum(len(plain(p).split()) for p in paras)
        st.update(title=item["title"], paragraphs=paras, hash=draft["hash"], src=item["url"],
                  meta=meta(item, max(1, round(words / 110))), voices=[])
        save_catalog(cat)
    old = {v["label"]: v for v in (st or {}).get("voices", [])}
    OUT.mkdir(exist_ok=True)
    if st is None:   # сказка в каталоге сразу, даже без озвучки — её озвучат по первому выбору
        words = sum(len(plain(p).split()) for p in paras)
        st = {"id": sid, "lang": lang, "title": item["title"],
              "meta": meta(item, max(1, round(words / 110))),
              "paragraphs": paras, "voices": [], "src": item["url"], "hash": draft["hash"]}
        cat["stories"].append(st)
        save_catalog(cat)

    for voice, gender in VOICES:
        vh = voice_hash(draft, voice)
        if old.get(voice, {}).get("hash") == vh:
            print(f"  {voice}: готово ранее")
            continue
        pcm = b"".join(synth("\n\n".join(g), draft.get("scene", ""), voice) for g in chunks(paras))
        mp3 = OUT / f"{lang}-{sid}-{voice}-{vh}.mp3"
        dur = to_mp3(pcm, mp3)
        upload(mp3)
        entry = {"name": f"{TTS_MODEL}/{voice}", "label": voice, "gender": gender, "hash": vh,
                 "weights": weights, "url": f"{BASE_URL}/{mp3.name}"}
        if st is None:
            st = {"id": sid, "lang": lang, "title": item["title"],
                  "meta": meta(item, max(1, round(dur / 60))),
                  "paragraphs": paras, "voices": [], "src": item["url"], "hash": draft["hash"]}
            cat["stories"].append(st)
        st.update(title=item["title"], paragraphs=paras, hash=draft["hash"], src=item["url"])
        st["voices"] = [v for v in st["voices"] if v["label"] != voice] + [entry]
        st["voices"].sort(key=lambda v: [x for x, _ in ALL_VOICES].index(v["label"])
                          if v["label"] in dict(ALL_VOICES) else 99)
        save_catalog(cat)                       # сохраняем после каждого голоса
        print(f"  {voice}: {dur:.0f} с")


def check() -> int:
    """Проверка всех источников library.json: открываются ли и сколько в них текста."""
    items = json.loads(LIBRARY.read_text(encoding="utf-8"))
    if ONLY:
        items = [i for i in items if i["id"] in ONLY]
    bad, lines = 0, []
    for it in items:
        try:
            t = fetch_source(it["url"])
            tail = " ".join(t.split())[-40:]
            lines.append(f"ok {it['id']} {len(t)} …{tail}")
        except Exception as e:
            bad += 1
            lines.append(f"ERR {it['id']}: {e}")
        time.sleep(1)
    print("\n".join(lines))
    # аннотация обрезается по длине — выводим порциями
    for i in range(0, len(lines), 12):
        print(f"::notice title=Источники {i + 1}–{min(i + 12, len(lines))} "
              f"(всего {len(items) - bad} ok, {bad} ошибок)::" + "%0A".join(lines[i:i + 12]))
    return 0


def main() -> int:
    if CHECK:
        return check()
    if not KEY:
        print("::error::Нет секрета GCP_TTS_KEY (Settings → Secrets and variables → Actions)")
        return 1
    items = json.loads(LIBRARY.read_text(encoding="utf-8"))
    cat = load_catalog()
    ids = [(i["lang"], i["id"]) for i in items]
    dup = {x for x in ids if ids.count(x) > 1}
    if dup:
        print(f"::error::повторы id в library.json: {sorted(dup)}")
        return 1

    pending = [i for i in items if (not ONLY or i["id"] in ONLY) and not is_done(cat, i)]
    print(f"в библиотеке {len(items)}, к обработке {len(pending)}, лимит {LIMIT or '—'}")
    done = failed = 0
    for item in pending:
        if LIMIT and done >= LIMIT:
            break
        print(f"▶ {item['lang']}/{item['id']} — {item['title']}")
        try:
            process(cat, item)
            done += 1
        except Stop as e:
            print(f"::warning::Квота/баланс исчерпаны, сохраняю сделанное и останавливаюсь. {str(e)[:300]}")
            break
        except Exception as e:
            failed += 1
            print(f"::warning::{item['id']}: пропущено — {str(e)[:300]}")
    left = sum(1 for i in items if not is_done(cat, i))
    print(f"Готово: {done}, ошибок: {failed}, осталось в библиотеке: {left}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"Готово: **{done}**, ошибок: {failed}, осталось: {left}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
