# skazka-content

Каталог и озвучка сказок для приложения «Сказка на ночь».

- `catalog.json` — список сказок, приложение берёт его отсюда:
  https://raw.githubusercontent.com/axiles126/skazka-content/main/catalog.json
- озвучка — файлы релиза **audio**, ссылки на них прописаны в каталоге.

## Библиотека

`library.json` — список сказок `{id, lang, title, url}`. Добавить сказку = добавить строку.
На push workflow **build-library** обрабатывает до 2 новых сказок: текст с сайта →
Gemini (полный текст, возраст 4–5, спокойный тон) → озвучка 4 голосами → релиз `audio`
→ `catalog.json`. Готовое пропускается по хешу, тексты кешируются в `drafts/`.
Квота кончилась — сделанное сохраняется, следующий прогон продолжит.

Порция вручную:

    gh workflow run build-library.yml -R axiles126/skazka-content -f limit=5

Нужен секрет `GCP_TTS_KEY`.

Встроенные в APK сказки публикуются из репозитория приложения (`tools/publish_content.py`) —
`incoming/*.mp3` workflow `publish-audio` переносит в релиз.
