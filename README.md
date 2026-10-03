# skazka-content

Каталог и озвучка сказок для приложения «Сказка на ночь».

- `catalog.json` — список сказок, приложение берёт его отсюда:
  https://raw.githubusercontent.com/axiles126/skazka-content/main/catalog.json
- озвучка — файлы релиза **audio**, ссылки на них прописаны в каталоге.
  Новые MP3 кладутся в `incoming/`, workflow `publish-audio` сам переносит их в релиз.

Тексты — народные сказки (общественное достояние), пересказ свой.
Содержимое публикуется из репозитория приложения скриптом `tools/publish_content.py`.
