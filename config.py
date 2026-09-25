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


def _get_str_list(name: str) -> Optional[List[str]]:
    parsed = _get_list(name)
    if parsed is None:
        return None
    return [str(item).strip() for item in parsed if str(item).strip()]


# Встроенный список тем фото-тестов (используется, если PHOTO_THEMES не задан).
DEFAULT_PHOTO_THEMES = [
    "животные",
    "растения",
    "достопримечательности и места",
    "картины",
    "советские фильмы",
    "иностранные фильмы",
    "актёры",
]

# Источники изображений по умолчанию (порядок важен).
DEFAULT_PHOTO_SOURCES = ["wikimedia", "openverse"]

KNOWN_PHOTO_SOURCES = (
    "wikimedia",
    "openverse",
    "ruwiki_film",
    "filmgrab",
    "movscreencaps",
)


@dataclass
class Config:
    # --- Модель (LLM) ---
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_temperature: float
    llm_timeout: int
    llm_retries: int
    llm_retry_delay: float

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
    announce_templates: Optional[List[str]]

    # --- Состояние (защита от дублей) ---
    state_file: str
    force: bool

    # --- Режим работы ---
    # "erudition" — обычный эрудиционный поток, "photo" — фото-тесты.
    mode: str = "erudition"

    # --- Фото-тесты (отдельный ежедневный поток) ---
    photo_flow_enabled: bool = False
    photo_survey_name: str = "Что на фото"
    photo_questions_count: int = 10
    photo_theme: Optional[str] = None
    photo_themes: Optional[List[str]] = None
    photo_sources: Optional[List[str]] = None
    photo_max_image_attempts: int = 3
    photo_min_questions: Optional[int] = None
    photo_image_timeout: int = 30
    photo_image_max_bytes: int = 8_000_000
    photo_state_file: str = "photo_state.json"
    wikimedia_user_agent: str = ""
    openverse_api_key: str = ""
    openverse_base_url: str = "https://api.openverse.org/v1"
    film_ru_enabled: bool = False
    film_grab_enabled: bool = False
    movie_screencaps_enabled: bool = False
    vision_api_key: str = ""
    vision_base_url: str = ""
    vision_model: str = ""
    vision_timeout: int = 120
    photo_vision_enabled: bool = False

    def validate(self, require_questions: bool = True) -> None:
        if self.mode == "photo":
            self._validate_photo(require_questions)
            return

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

    def _validate_photo(self, require_questions: bool = True) -> None:
        errors = []
        if not self.dry_run:
            if not self.yandex_token:
                errors.append("YANDEX_FORMS_TOKEN не задан")
            if not self.yandex_org_id:
                errors.append("YANDEX_ORG_ID (X-Org-Id / X-Cloud-Org-Id) не задан")
        if require_questions and self.photo_questions_count < 1:
            errors.append("PHOTO_QUESTIONS_COUNT должен быть >= 1")
        unknown_sources = [
            name for name in (self.photo_sources or []) if name not in KNOWN_PHOTO_SOURCES
        ]
        if unknown_sources:
            errors.append(
                "Неизвестные источники в PHOTO_SOURCES: "
                + ", ".join(unknown_sources)
                + ". Допустимые: "
                + ", ".join(KNOWN_PHOTO_SOURCES)
            )
        if require_questions and not self.llm_api_key:
            errors.append(
                "Для фото-потока нужен LLM_API_KEY (генерация сущностей и дистракторов)"
            )
        if require_questions and not self.enabled_photo_sources():
            errors.append(
                "Не включён ни один источник изображений "
                "(PHOTO_SOURCES или FILM_RU_ENABLED/FILM_GRAB_ENABLED/MOVIE_SCREENCAPS_ENABLED)"
            )
        if errors:
            raise ValueError("Ошибки конфигурации:\n  - " + "\n  - ".join(errors))

    def enabled_photo_sources(self) -> List[str]:
        """Активные источники изображений в порядке приоритета.

        wikimedia/openverse берутся из PHOTO_SOURCES (по умолчанию оба),
        кино-источники подключаются только по своим фича-флагам.
        """
        sources = (
            list(self.photo_sources)
            if self.photo_sources is not None
            else list(DEFAULT_PHOTO_SOURCES)
        )
        enabled = [s for s in ("wikimedia", "openverse") if s in sources]
        if self.film_ru_enabled:
            enabled.append("ruwiki_film")
        if self.film_grab_enabled:
            enabled.append("filmgrab")
        if self.movie_screencaps_enabled:
            enabled.append("movscreencaps")
        return enabled

    def effective_photo_themes(self) -> List[str]:
        if self.photo_themes is not None:
            return list(self.photo_themes)
        return list(DEFAULT_PHOTO_THEMES)

    def effective_photo_min_questions(self) -> int:
        if self.photo_min_questions is None:
            return self.photo_questions_count
        return self.photo_min_questions


def load_config(
    overrides: Optional[Dict[str, Any]] = None, require_questions: bool = True
) -> Config:
    """Собрать конфигурацию из окружения и применить переопределения (CLI)."""
    cfg = Config(
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip(),
        llm_temperature=_get_float("LLM_TEMPERATURE", 0.8),
        # Бюджет LLM: LLM_TIMEOUT * (LLM_RETRIES + 1 + 1) * max_attempts (main)
        # должен укладываться в timeout-minutes workflow. Дополнительный +1 —
        # возможный fallback-запрос на внешнюю попытку. При дефолтах
        # 120 * (1+1+1) * 3 = 1080 с = 18 мин < 25 мин (daily.yml).
        # Фото-поток делает много вызовов на выпуск, его бюджет считается
        # отдельно (см. .github/workflows/daily_photo.yml).
        llm_timeout=_get_int("LLM_TIMEOUT", 120) or 120,
        llm_retries=max(0, _get_int("LLM_RETRIES", 1) or 0),
        llm_retry_delay=max(0.0, _get_float("LLM_RETRY_DELAY", 5.0)),
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
        announce_template=os.getenv("ANNOUNCE_TEMPLATE", "").strip(),
        announce_templates=_get_list("ANNOUNCE_TEMPLATES"),
        state_file=os.getenv("STATE_FILE", "state.json").strip(),
        force=_get_bool("FORCE", False),
        # --- Фото-тесты ---
        photo_flow_enabled=_get_bool("PHOTO_FLOW_ENABLED", False),
        photo_survey_name=os.getenv("PHOTO_SURVEY_NAME", "Что на фото").strip(),
        photo_questions_count=_get_int("PHOTO_QUESTIONS_COUNT", 10),
        photo_theme=(os.getenv("PHOTO_THEME") or "").strip() or None,
        photo_themes=_get_str_list("PHOTO_THEMES"),
        photo_sources=_get_str_list("PHOTO_SOURCES"),
        photo_max_image_attempts=_get_int("PHOTO_MAX_IMAGE_ATTEMPTS", 3),
        photo_min_questions=_get_int("PHOTO_MIN_QUESTIONS", None),
        photo_image_timeout=_get_int("PHOTO_IMAGE_TIMEOUT", 30),
        photo_image_max_bytes=_get_int("PHOTO_IMAGE_MAX_BYTES", 8_000_000),
        photo_state_file=os.getenv("PHOTO_STATE_FILE", "photo_state.json").strip(),
        wikimedia_user_agent=os.getenv("WIKIMEDIA_USER_AGENT", "").strip(),
        openverse_api_key=os.getenv("OPENVERSE_API_KEY", "").strip(),
        openverse_base_url=os.getenv(
            "OPENVERSE_BASE_URL", "https://api.openverse.org/v1"
        ).rstrip("/"),
        film_ru_enabled=_get_bool("FILM_RU_ENABLED", False),
        film_grab_enabled=_get_bool("FILM_GRAB_ENABLED", False),
        movie_screencaps_enabled=_get_bool("MOVIE_SCREENCAPS_ENABLED", False),
        photo_vision_enabled=_get_bool("PHOTO_VISION_ENABLED", False),
        vision_timeout=_get_int("VISION_TIMEOUT", 120),
    )

    # Vision может использовать отдельный ключ/URL/модель, иначе — параметры LLM.
    cfg.vision_api_key = (os.getenv("VISION_API_KEY") or "").strip() or cfg.llm_api_key
    cfg.vision_base_url = (
        (os.getenv("VISION_BASE_URL") or "").strip() or cfg.llm_base_url
    ).rstrip("/")
    cfg.vision_model = (os.getenv("VISION_MODEL") or "").strip() or cfg.llm_model

    if overrides:
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)

    # Если минимум не задан явно — он равен числу вопросов.
    if cfg.photo_min_questions is None:
        cfg.photo_min_questions = cfg.photo_questions_count

    cfg.validate(require_questions=require_questions)
    return cfg
