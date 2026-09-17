# AGENTS.md

Руководство для AI-агентов, работающих в этом репозитории.

## О проекте

Автоматическое создание и публикация тестов-викторин в Яндекс Формах.
Приложение генерирует вопросы на общую эрудицию через LLM (OpenAI-совместимый
API), собирает из них тест с правильными ответами, перемешиванием вариантов и
сегментами результатов, публикует форму и анонсирует её в Telegram и VK.
Запускается ежедневно через GitHub Actions.

## Структура

- `main.py` — точка входа и CLI (`--dry-run`, `--check`, `--force`, `--delete-survey`).
- `config.py` — загрузка настроек из переменных окружения / `.env`.
- `llm.py` — генерация вопросов через LLM.
- `yandex_forms.py` — клиент API Яндекс Форм (создание формы, вопросы, публикация).
- `publisher.py` — публикация анонса в Telegram (Bot API) и VK (Wall API).
- `state.py` — состояние и защита от повторной публикации в один день.
- `samples/questions.json` — пример готовых вопросов (запуск без LLM).
- `state.json` — состояние: дата последней публикации и id форм.
- `.github/workflows/daily.yml` — планировщик ежедневного запуска.
- `requirements.txt` — зависимости Python.

## Запуск локально

Нужен Python 3.11+ и переменные окружения (или файл `.env` по образцу
`.env.example`):

- `LLM_API_KEY` — ключ модели (например, AITunnel: `sk-aitunnel-…`).
- `LLM_BASE_URL` — адрес API модели (по умолчанию `https://api.openai.com/v1`,
  для AITunnel — `https://api.aitunnel.ru/v1`).
- `LLM_MODEL` — название модели.
- `YANDEX_FORMS_TOKEN` — OAuth-токен с правом `forms:write`.
- `YANDEX_ORG_ID` — идентификатор организации (заголовок `X-Cloud-Org-Id`).
- `TG_BOT_TOKEN`, `TG_TARGET_CHANNEL` — для анонса в Telegram (опционально).
- `VK_ACCESS_TOKEN`, `VK_GROUP_ID` — для анонса в VK (опционально).

```bash
pip install -r requirements.txt
python main.py --dry-run          # проверка без обращения к API
python main.py --check             # проверить доступ к Яндекс Формам
python main.py                     # создать и опубликовать форму
python main.py --questions-file samples/questions.json
```

## Секреты в CI

`.github/workflows/daily.yml` использует GitHub Secrets:
`LLM_API_KEY`, `YANDEX_FORMS_TOKEN`, `YANDEX_ORG_ID`,
а также `TG_BOT_TOKEN`, `TG_TARGET_CHANNEL`, `VK_ACCESS_TOKEN`, `VK_GROUP_ID`
(для анонса в соцсетях). Несекретные настройки задаются через Variables.

## Соглашения

- Комментарии и код на русском / английском вперемешку — сохраняйте существующий стиль.
- Не коммитьте секреты и `.env` в git; для ключей — только GitHub Secrets.
- `state.json` обновляется приложением и коммитится workflow (не вручную).
- Для изменений в расписании правится `.github/workflows/daily.yml` (cron в UTC).
- Новые настройки добавляйте в `config.py`, `.env.example` и README (таблица переменных).
- Перед изменениями в API Яндекс Форм сверяйтесь с
  https://yandex.ru/support/forms/ru/api-ref/.
- Токен Яндекс Форм даёт полный доступ ко всем формам — обращайтесь с ним аккуратно.