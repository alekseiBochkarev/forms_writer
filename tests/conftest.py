"""Общая конфигурация pytest: путь импорта и изоляция переменных окружения."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Переменные окружения, которые читает config.load_config. Перед каждым тестом
# очищаются, чтобы тесты не зависели от окружения машины/CI и от порядка запуска.
_APP_ENV_VARS = [
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
    "LLM_TIMEOUT",
    "LLM_RETRIES",
    "LLM_RETRY_DELAY",
    "YANDEX_FORMS_TOKEN",
    "YANDEX_ORG_ID",
    "YANDEX_ORG_HEADER",
    "YANDEX_SURVEY_ID",
    "QUESTIONS_TOPIC",
    "QUESTIONS_COUNT",
    "QUESTIONS_LANGUAGE",
    "SURVEY_NAME",
    "PUBLISH",
    "NUMBER_SURVEYS",
    "SHUFFLE",
    "SHOW_RESULTS",
    "SHOW_CORRECT",
    "STATS",
    "PASS_SCORES",
    "SEGMENTS",
    "CLEAR_EXISTING",
    "QUESTIONS_FILE",
    "DRY_RUN",
    "PUBLISH_TELEGRAM",
    "TG_BOT_TOKEN",
    "TG_TARGET_CHANNEL",
    "PUBLISH_VK",
    "VK_ACCESS_TOKEN",
    "VK_GROUP_ID",
    "ANNOUNCE_TEMPLATE",
    "ANNOUNCE_TEMPLATES",
    "STATE_FILE",
    "FORCE",
    "PHOTO_FLOW_ENABLED",
    "PHOTO_SURVEY_NAME",
    "PHOTO_QUESTIONS_COUNT",
    "PHOTO_THEME",
    "PHOTO_THEMES",
    "PHOTO_SOURCES",
    "PHOTO_MAX_IMAGE_ATTEMPTS",
    "PHOTO_MIN_QUESTIONS",
    "PHOTO_IMAGE_TIMEOUT",
    "PHOTO_IMAGE_MAX_BYTES",
    "PHOTO_STATE_FILE",
    "WIKIMEDIA_USER_AGENT",
    "OPENVERSE_API_KEY",
    "OPENVERSE_BASE_URL",
    "FILM_RU_ENABLED",
    "FILM_GRAB_ENABLED",
    "MOVIE_SCREENCAPS_ENABLED",
    "PHOTO_VISION_ENABLED",
    "VISION_TIMEOUT",
    "VISION_API_KEY",
    "VISION_BASE_URL",
    "VISION_MODEL",
]


@pytest.fixture(autouse=True)
def _clean_app_env(monkeypatch: pytest.MonkeyPatch):
    """Убрать все переменные приложения, чтобы тесты были герметичными."""
    for name in _APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield