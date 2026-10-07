"""Тесты config.py: дефолты фото-настроек, режим photo и fallback Vision->LLM."""

from __future__ import annotations

import pytest

import config
from config import (
    DEFAULT_PHOTO_SOURCES,
    DEFAULT_PHOTO_THEMES,
    KNOWN_PHOTO_SOURCES,
    load_config,
)


# --- Дефолты фото-настроек -------------------------------------------------


def test_photo_defaults_when_env_empty():
    """Без переменных окружения фото-настройки берут встроенные значения."""
    cfg = load_config({"dry_run": True}, require_questions=False)

    assert cfg.mode == "erudition"
    assert cfg.photo_flow_enabled is False
    assert cfg.photo_survey_name == "Что на фото"
    assert cfg.photo_questions_count == 10
    assert cfg.photo_theme is None
    assert cfg.photo_themes is None
    assert cfg.photo_sources is None
    assert cfg.photo_max_image_attempts == 3
    assert cfg.photo_image_timeout == 30
    assert cfg.photo_image_max_bytes == 8_000_000
    assert cfg.photo_state_file == "photo_state.json"
    assert cfg.openverse_base_url == "https://api.openverse.org/v1"
    assert cfg.photo_vision_enabled is True
    assert cfg.vision_timeout == 120


def test_effective_photo_themes_and_min_questions_defaults():
    """effective_* подставляют встроенные темы и минимум = число вопросов."""
    cfg = load_config({"dry_run": True}, require_questions=False)

    assert cfg.effective_photo_themes() == DEFAULT_PHOTO_THEMES
    # темы — копия, а не ссылка на константу
    cfg.effective_photo_themes().append("x")
    assert "x" not in DEFAULT_PHOTO_THEMES

    assert cfg.effective_photo_min_questions() == cfg.photo_questions_count == 10


def test_default_photo_themes_include_soviet_people_and_films():
    """Во встроенных темах есть советские фильмы, актёры и актрисы."""
    assert "советские фильмы" in DEFAULT_PHOTO_THEMES
    assert "советские актёры" in DEFAULT_PHOTO_THEMES
    assert "советские актрисы" in DEFAULT_PHOTO_THEMES


def test_enabled_photo_sources_defaults_and_flags():
    """wikimedia/openverse — по умолчанию; кино-источники только по флагам."""
    cfg = load_config({"dry_run": True}, require_questions=False)
    assert cfg.enabled_photo_sources() == DEFAULT_PHOTO_SOURCES

    cfg = load_config(
        {"dry_run": True, "film_ru_enabled": True, "movie_screencaps_enabled": True},
        require_questions=False,
    )
    assert cfg.enabled_photo_sources() == [
        "wikimedia",
        "openverse",
        "ruwiki_film",
        "movscreencaps",
    ]


def test_photo_themes_override_list():
    """PHOTO_THEMES из окружения переопределяет встроенный список тем."""
    import os

    os.environ["PHOTO_THEMES"] = '["космос", "  ", "музыка"]'
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["PHOTO_THEMES"]

    assert cfg.effective_photo_themes() == ["космос", "музыка"]


# --- Валидация режима photo -------------------------------------------------


def test_photo_mode_requires_llm_api_key():
    """mode=photo без LLM_API_KEY — понятная ошибка валидации."""
    with pytest.raises(ValueError) as exc:
        load_config({"mode": "photo", "dry_run": True})

    assert "LLM_API_KEY" in str(exc.value)


def test_photo_mode_unknown_source_rejected():
    """Неизвестное имя в PHOTO_SOURCES — ошибка с перечислением допустимых."""
    import os

    os.environ["PHOTO_SOURCES"] = '["wikimedia", "unknown-source"]'
    os.environ["LLM_API_KEY"] = "k"
    try:
        with pytest.raises(ValueError) as exc:
            load_config({"mode": "photo", "dry_run": True})
    finally:
        del os.environ["PHOTO_SOURCES"]
        del os.environ["LLM_API_KEY"]

    message = str(exc.value)
    assert "unknown-source" in message
    # перечислены все допустимые источники
    for source in KNOWN_PHOTO_SOURCES:
        assert source in message


def test_photo_mode_empty_sources_rejected():
    """Пустой список PHOTO_SOURCES (и выключенные кино-флаги) — ошибка."""
    import os

    os.environ["PHOTO_SOURCES"] = "[]"
    os.environ["LLM_API_KEY"] = "k"
    try:
        with pytest.raises(ValueError) as exc:
            load_config({"mode": "photo", "dry_run": True})
    finally:
        del os.environ["PHOTO_SOURCES"]
        del os.environ["LLM_API_KEY"]

    assert "источник изображений" in str(exc.value)


def test_photo_mode_valid_config_passes():
    """Полностью корректная фото-конфигурация валидируется без ошибок."""
    cfg = load_config(
        {"mode": "photo", "dry_run": True, "llm_api_key": "k"},
    )
    assert cfg.mode == "photo"
    assert cfg.enabled_photo_sources() == DEFAULT_PHOTO_SOURCES


def test_photo_mode_questions_count_must_be_positive():
    """PHOTO_QUESTIONS_COUNT < 1 — ошибка валидации фото-режима."""
    with pytest.raises(ValueError) as exc:
        load_config(
            {
                "mode": "photo",
                "dry_run": True,
                "llm_api_key": "k",
                "photo_questions_count": 0,
            },
        )
    assert "PHOTO_QUESTIONS_COUNT" in str(exc.value)


# --- Vision fallback на LLM ------------------------------------------------


def test_vision_falls_back_to_llm_settings():
    """Без VISION_* vision использует LLM_* настройки."""
    import os

    os.environ["LLM_API_KEY"] = "llm-key"
    os.environ["LLM_BASE_URL"] = "https://llm.example/v1/"
    os.environ["LLM_MODEL"] = "llm-model"
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
            del os.environ[name]

    assert cfg.vision_api_key == "llm-key"
    # base_url без завершающего слэша
    assert cfg.vision_base_url == "https://llm.example/v1"
    assert cfg.vision_model == "llm-model"


def test_vision_overrides_llm_settings():
    """Заданные VISION_* имеют приоритет над LLM_*."""
    import os

    os.environ["LLM_API_KEY"] = "llm-key"
    os.environ["LLM_BASE_URL"] = "https://llm.example/v1"
    os.environ["LLM_MODEL"] = "llm-model"
    os.environ["VISION_API_KEY"] = "vision-key"
    os.environ["VISION_BASE_URL"] = "https://vision.example/v1/"
    os.environ["VISION_MODEL"] = "vision-model"
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        for name in (
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "VISION_API_KEY",
            "VISION_BASE_URL",
            "VISION_MODEL",
        ):
            del os.environ[name]

    assert cfg.vision_api_key == "vision-key"
    assert cfg.vision_base_url == "https://vision.example/v1"
    assert cfg.vision_model == "vision-model"


# --- Эрудиционная валидация не сломана -------------------------------------


def test_erudition_requires_llm_or_questions_file():
    """Режим эрудиции по-прежнему требует LLM_API_KEY либо QUESTIONS_FILE."""
    with pytest.raises(ValueError) as exc:
        load_config({"dry_run": True})

    message = str(exc.value)
    assert "LLM_API_KEY" in message
    assert "QUESTIONS_FILE" in message


def test_erudition_count_validated():
    """QUESTIONS_COUNT < 1 — ошибка в эрудиционном режиме."""
    with pytest.raises(ValueError) as exc:
        load_config(
            {"dry_run": True, "llm_api_key": "k", "count": 0}
        )
    assert "QUESTIONS_COUNT" in str(exc.value)


def test_erudition_valid_with_questions_file():
    """Эрудиция с готовым файлом вопросов и dry-run валидна без LLM."""
    cfg = load_config(
        {"dry_run": True, "questions_file": "samples/questions.json"},
    )
    assert cfg.mode == "erudition"
    assert cfg.questions_file == "samples/questions.json"


# --- Вводная/заключительная и ревью -----------------------------------------


def test_intro_outro_and_review_defaults():
    """Настройки вводной/заключительной/ревью имеют ожидаемые дефолты."""
    cfg = load_config({"dry_run": True}, require_questions=False)

    assert cfg.intro_enabled is True
    assert cfg.conclusion_enabled is True
    assert cfg.review_enabled is True
    assert cfg.review_max_attempts == 1
    assert cfg.intro_min_paragraphs == 2
    assert cfg.intro_max_paragraphs == 4
    assert cfg.outro_min_paragraphs == 1
    assert cfg.outro_max_paragraphs == 3
    assert cfg.announce_intro is True
    assert cfg.effective_review_model() == cfg.llm_model


def test_review_model_override():
    """REVIEW_MODEL переопределяет модель ревью."""
    import os

    os.environ["REVIEW_MODEL"] = "review-model"
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["REVIEW_MODEL"]
    assert cfg.effective_review_model() == "review-model"


# --- Поток новых тестов ------------------------------------------------------


def test_new_tests_defaults():
    """Новый поток по умолчанию выключен, 15 вопросов, канал qa_helper_draft."""
    cfg = load_config({"dry_run": True}, require_questions=False)

    assert cfg.new_tests_enabled is False
    assert cfg.new_tests_count == 15
    assert cfg.effective_new_tests_pass_scores() == [10]
    assert cfg.effective_new_tests_min_questions() == 15
    assert cfg.new_tests_state_file == "new_state.json"
    assert cfg.new_tests_tg_target_channel == "@qa_helper_draft"
    assert cfg.new_tests_publish_vk is False


def test_new_tests_pass_scores_parsing():
    """NEW_TESTS_PASS_SCORES принимает JSON-массив и строку «7,8»."""
    import os

    os.environ["NEW_TESTS_PASS_SCORES"] = "[8, 9]"
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_PASS_SCORES"]
    assert cfg.effective_new_tests_pass_scores() == [8, 9]

    os.environ["NEW_TESTS_PASS_SCORES"] = "7, 8"
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_PASS_SCORES"]
    assert cfg.effective_new_tests_pass_scores() == [7, 8]


def test_new_tests_mode_requires_llm_key():
    """mode=new_tests без LLM_API_KEY — понятная ошибка валидации."""
    with pytest.raises(ValueError) as exc:
        load_config({"mode": "new_tests", "dry_run": True})

    assert "LLM_API_KEY" in str(exc.value)


def test_new_tests_mode_valid_config_passes():
    cfg = load_config({"mode": "new_tests", "dry_run": True, "llm_api_key": "k"})
    assert cfg.mode == "new_tests"


def test_new_tests_count_zero_is_rejected():
    """NEW_TESTS_COUNT=0 не превращается в 10, а даёт ошибку валидации."""
    import os

    os.environ["NEW_TESTS_COUNT"] = "0"
    try:
        with pytest.raises(ValueError) as exc:
            load_config({"mode": "new_tests", "dry_run": True, "llm_api_key": "k"})
    finally:
        del os.environ["NEW_TESTS_COUNT"]
    assert "NEW_TESTS_COUNT" in str(exc.value)


def test_default_tg_target_channel_points_to_qa_helper_draft():
    cfg = load_config({"dry_run": True}, require_questions=False)
    assert cfg.tg_target_channel == "@qa_helper_draft"


def test_default_photo_themes_narrowed_but_keeps_soviet():
    """Темы сужены до подгрупп, советское кино сохранено (обратная совместимость)."""
    assert "породы собак" in DEFAULT_PHOTO_THEMES
    assert "картины из школьной программы" in DEFAULT_PHOTO_THEMES
    assert "советские фильмы" in DEFAULT_PHOTO_THEMES
    assert "животные" not in DEFAULT_PHOTO_THEMES
    assert "картины" not in DEFAULT_PHOTO_THEMES


# --- NEW_TESTS_TOPICS: крайние случаи парсинга -------------------------------


def test_new_tests_topics_empty_list_yields_no_topics():
    """NEW_TESTS_TOPICS=[] — явно пустой список, а не встроенные темы.

    `effective_new_tests_topics` должен вернуть пустой список (None — только
    когда переменная не задана), иначе поток попытается сгенерировать выпуск
    по встроенным темам вместо настроенных.
    """
    import os

    os.environ["NEW_TESTS_TOPICS"] = "[]"
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_TOPICS"]

    assert cfg.new_tests_topics == []
    assert cfg.effective_new_tests_topics() == []


def test_new_tests_topics_broken_json_raises():
    """Битый JSON в NEW_TESTS_TOPICS — ValueError, а не тихий дефолт."""
    import os

    os.environ["NEW_TESTS_TOPICS"] = "не json"
    try:
        with pytest.raises(ValueError):
            load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_TOPICS"]


def test_new_tests_topics_non_list_json_raises():
    """NEW_TESTS_TOPICS как JSON-объект — ошибка формата."""
    import os

    os.environ["NEW_TESTS_TOPICS"] = '{"тема": "кино"}'
    try:
        with pytest.raises(ValueError) as exc:
            load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_TOPICS"]

    assert "JSON-массив" in str(exc.value)


# --- NEW_TESTS_PASS_SCORES: форматы -----------------------------------------


def test_new_tests_pass_scores_single_number():
    """Одиночное число (без массива) — валидный порог."""
    import os

    os.environ["NEW_TESTS_PASS_SCORES"] = "7"
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_PASS_SCORES"]
    assert cfg.effective_new_tests_pass_scores() == [7]


def test_new_tests_pass_scores_empty_string_uses_defaults():
    """Пустая строка не задаёт порог — берётся встроенный 10."""
    import os

    os.environ["NEW_TESTS_PASS_SCORES"] = ""
    try:
        cfg = load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_PASS_SCORES"]
    assert cfg.effective_new_tests_pass_scores() == [10]


def test_new_tests_pass_scores_garbage_raises():
    """Мусор вместо чисел — ValueError."""
    import os

    os.environ["NEW_TESTS_PASS_SCORES"] = "abc"
    try:
        with pytest.raises(ValueError):
            load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_PASS_SCORES"]


def test_new_tests_pass_scores_empty_array_raises():
    """Пустой массив не оставляет ни одного порога — ошибка."""
    import os

    os.environ["NEW_TESTS_PASS_SCORES"] = "[]"
    try:
        with pytest.raises(ValueError):
            load_config({"dry_run": True}, require_questions=False)
    finally:
        del os.environ["NEW_TESTS_PASS_SCORES"]