"""Загрузка и проверка настроек приложения.

Все настройки берутся из переменных окружения (или файла .env при локальном
запуске). Для GitHub Actions значения задаются через Secrets.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv опционален
    pass


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


def _get_int(name: str, default: Optional[int]) -> Optional[int]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def _get_list(name: str) -> Optional[List[Dict[str, Any]]]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError(f"{name} должен быть JSON-массивом")
    return parsed


@dataclass
class Config:
    # --- Модель (LLM) ---
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_temperature: float

    # --- Яндекс Формы ---
    yandex_token: str
    yandex_org_id: str
    yandex_org_header: str
    yandex_survey_id: Optional[str]

    # --- Содержание теста ---
    topic: str
    count: int
    language: str
    survey_name: str

    # --- Поведение ---
    publish: bool
    number_surveys: bool
    shuffle: bool
    show_results: bool
    show_correct: bool
    stats: bool
    pass_scores: Optional[int]
    segments: Optional[List[Dict[str, Any]]]
    clear_existing: bool
    questions_file: Optional[str]
    dry_run: bool

    # --- Публикация анонса в соцсетях ---
    publish_telegram: bool
    tg_bot_token: str
    tg_target_channel: str
    publish_vk: bool
    vk_access_token: str
    vk_group_id: str
    announce_template: str

    # --- Состояние (защита от дублей) ---
    state_file: str
    force: bool

    def validate(self, require_questions: bool = True) -> None:
        errors = []
        if not self.dry_run:
            if not self.yandex_token:
                errors.append("YANDEX_FORMS_TOKEN не задан")
            if not self.yandex_org_id:
                errors.append("YANDEX_ORG_ID (X-Org-Id / X-Cloud-Org-Id) не задан")
        if require_questions and not self.questions_file and not self.llm_api_key:
            errors.append(
                "Нужен либо LLM_API_KEY (генерация вопросов), либо QUESTIONS_FILE (готовый файл)"
            )
        if require_questions and self.count < 1:
            errors.append("QUESTIONS_COUNT должен быть >= 1")
        if errors:
            raise ValueError("Ошибки конфигурации:\n  - " + "\n  - ".join(errors))


def load_config(
    overrides: Optional[Dict[str, Any]] = None, require_questions: bool = True
) -> Config:
    """Собрать конфигурацию из окружения и применить переопределения (CLI)."""
    cfg = Config(
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip(),
        llm_temperature=_get_float("LLM_TEMPERATURE", 0.8),
        yandex_token=os.getenv("YANDEX_FORMS_TOKEN", "").strip(),
        yandex_org_id=os.getenv("YANDEX_ORG_ID", "").strip(),
        yandex_org_header=os.getenv("YANDEX_ORG_HEADER", "X-Cloud-Org-Id").strip(),
        yandex_survey_id=(os.getenv("YANDEX_SURVEY_ID") or "").strip() or None,
        topic=os.getenv("QUESTIONS_TOPIC", "общая эрудиция").strip(),
        count=_get_int("QUESTIONS_COUNT", 13) or 13,
        language=os.getenv("QUESTIONS_LANGUAGE", "ru").strip(),
        survey_name=os.getenv(
            "SURVEY_NAME", "Насколько широк ваш кругозор"
        ).strip(),
        publish=_get_bool("PUBLISH", True),
        number_surveys=_get_bool("NUMBER_SURVEYS", True),
        shuffle=_get_bool("SHUFFLE", True),
        show_results=_get_bool("SHOW_RESULTS", True),
        show_correct=_get_bool("SHOW_CORRECT", True),
        stats=_get_bool("STATS", True),
        pass_scores=_get_int("PASS_SCORES", None),
        segments=_get_list("SEGMENTS"),
        clear_existing=_get_bool("CLEAR_EXISTING", False),
        questions_file=(os.getenv("QUESTIONS_FILE") or "").strip() or None,
        dry_run=_get_bool("DRY_RUN", False),
        publish_telegram=_get_bool("PUBLISH_TELEGRAM", True),
        tg_bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
        tg_target_channel=os.getenv("TG_TARGET_CHANNEL", "").strip(),
        publish_vk=_get_bool("PUBLISH_VK", True),
        vk_access_token=os.getenv("VK_ACCESS_TOKEN", "").strip(),
        vk_group_id=os.getenv("VK_GROUP_ID", "").strip(),
        announce_template=os.getenv(
            "ANNOUNCE_TEMPLATE",
            "Новый тест: «{name}»\n\n{count} вопросов на разные темы — проверьте свой кругозор.\n\nПройти: {url}",
        ).strip(),
        state_file=os.getenv("STATE_FILE", "state.json").strip(),
        force=_get_bool("FORCE", False),
    )

    if overrides:
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)

    cfg.validate(require_questions=require_questions)
    return cfg