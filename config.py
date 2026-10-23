"""Загрузка и проверка настроек приложения.

Все настройки берутся из переменных окружения (или файла ..env при локальном
запуске). Для GitHub Actions значения задаются через Secrets.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
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


def _get_int_safe(name: str, default: int) -> int:
    """Целое из переменной окружения; пустое или нечисловое значение -> default.

    В отличие от `_get_int`, не падает на мусорном значении: используется для
    необязательных порогов, где дефолт безопаснее ошибки загрузки конфига.
    """
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


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


def _get_dict_of_str_lists(name: str) -> Optional[Dict[str, List[str]]]:
    """JSON-объект вида {"категория": ["шаблон", ...]} из переменной окружения."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} должен быть JSON-объектом")
    result: Dict[str, List[str]] = {}
    for key, templates in parsed.items():
        if not isinstance(templates, list):
            raise ValueError(f"{name}: значение «{key}» должно быть массивом строк")
        result[str(key).strip()] = [
            str(item).strip() for item in templates if str(item).strip()
        ]
    return result


def _get_int_list(name: str) -> Optional[List[int]]:
    """Список целых из JSON-массива или строки вида «7,8» ("7" -> [7])."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    value = value.strip()
    if value.startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError(f"{name} должен быть JSON-массивом чисел")
        items = parsed
    else:
        items = value.split(",")
    result = [int(item) for item in items if str(item).strip()]
    if not result:
        raise ValueError(f"{name} не содержит чисел")
    return result


# Встроенный список тем фото-тестов (используется, если PHOTO_THEMES не задан).
# По данным исследования каналов Дзена узкие узнаваемые подгруппы (советское кино,
# актёры, конкретные группы животных) читаются лучше родовых тем, поэтому список
# сужен до узнаваемых подгрупп. Советское кино — в приоритете.
DEFAULT_PHOTO_THEMES = [
    "советские фильмы",
    "советские актёры",
    "советские актрисы",
    "иностранные фильмы",
    "породы собак",
    "хищные птицы",
    "животные Африки",
    "картины из школьной программы",
    "пейзажи русских художников",
    "достопримечательности и места",
]

# Шаблоны заголовков фото-тестов по категориям (формула §10.3:
# [ставка/идентичность] + [конкретная категория] + [число]). {count} —
# число вопросов выпуска. Ключи задают категорию, а не точную тему: тема
# выпуска сопоставляется с категорией по регулярным выражениям (photo_flow).
DEFAULT_PHOTO_TITLE_TEMPLATES: Dict[str, List[str]] = {
    "советские фильмы": [
        "Узнайте советский фильм по одному кадру. {count} вопросов",
        "Вы смотрели эти {count} советских фильмов, если узнаете их по кадру",
        "Какому советскому фильму принадлежит этот кадр? Тест на {count} вопросов",
        "Сможете узнать каждый из {count} советских фильмов по кадру?",
        "Вы настоящий киноман СССР, если ответите верно на все {count} вопросов по кадрам",
    ],
    "советские актёры": [
        "Сможете вспомнить советского актёра по трём фильмам? {count} вопросов",
        "Узнаете ли вы всех этих советских актёров по фото? Тест из {count} вопросов",
        "Вы настоящий знаток советского кино, если узнаете всех {count} актёров",
        "Только 1 из {count} узнаёт советских актёров по фото. Проверьте себя",
        "Назовите этих {count} советских актёров по фото. Сможете без подсказок?",
    ],
    "советские актрисы": [
        "Узнаете ли вы всех этих советских актрис по фото? Тест из {count} вопросов",
        "Вы настоящий киноман СССР, если узнаете всех {count} советских актрис",
        "Сможете назвать каждую из {count} советских актрис по фото?",
        "Только 1 из {count} узнаёт советских актрис по фото. Проверьте себя",
    ],
    "иностранные фильмы": [
        "Узнайте культовый фильм по одному кадру. Тест из {count} вопросов",
        "Только настоящий киноман назовёт все {count} фильмов по кадру. А вы?",
        "Сможете узнать каждый из {count} культовых фильмов по кадру?",
        "Узнаете ли вы эти {count} фильмов по одному кадру? Тест для киноманов",
    ],
    "актёры": [
        "Узнаете ли вы этих {count} актёров по фото? Тест на внимательность",
        "Сможете назвать всех {count} актёров на фото?",
        "Только 1 из {count} узнаёт этих актёров по фото. Проверьте себя",
    ],
    "породы собак": [
        "Породы собак: узнаете ли вы каждую из {count} по фото?",
        "Вы настоящий собаковод, если назовёте все {count} пород собак",
        "Узнаете ли вы этих {count} собак по одной фотографии?",
        "Только 1 из {count} отличает эти породы собак по фото. А вы?",
    ],
    "хищные птицы": [
        "Узнаете ли вы этих {count} хищных птиц по фото? Тест на внимательность",
        "Вы настоящий знаток природы, если назовёте всех {count} птиц на фото",
        "Только 1 из {count} узнаёт этих птиц по фото. Проверьте себя",
    ],
    "животные": [
        "Узнаете ли вы этих {count} животных по одной фотографии? Тест на внимательность",
        "Вы настоящий знаток природы, если назовёте всех {count} животных на фото",
        "Только 1 из {count} узнаёт этих животных по глазам. Проверьте себя",
    ],
    "растения": [
        "Вы отличаете лекарственные растения от сорняков? Тест из {count} фото",
        "Назовите эти {count} цветов по фото. Сможете без подсказок?",
        "Узнаете ли вы эти {count} растения по одной фотографии?",
    ],
    "картины": [
        "Вы помните эти {count} картин из школьной программы? Проверьте по фото",
        "Узнаете ли вы эти {count} шедевров живописи по фрагменту?",
        "Тест для знатоков искусства: назовите автора этих {count} картин",
    ],
    "достопримечательности": [
        "Узнаете ли вы эти {count} городов мира по одной фотографии?",
        "Вы путешественник, если узнаете эти {count} достопримечательностей без подсказок",
        "Угадайте {count} стран по фото. Без ошибок справятся только эрудиты",
    ],
    "generic": [
        "Узнаете ли вы все {count} на фото? Тест на внимательность",
        "Только 1 из {count} угадает всё на этих фото. Проверьте себя",
        "Проверьте себя: назовёте ли вы все {count} по фото?",
    ],
}

# Встроенные темы потока «новых тестов»: общая эрудиция + наиболее популярные
# направления по данным исследования Дзена §2/§9 (в первую очередь
# ностальгические и узнаваемые).
DEFAULT_NEW_TESTS_TOPICS = [
    "общая эрудиция",
    "творческая личность",
    "советское кино",
    "советские актёры",
    "русские народные сказки",
    "кулинария",
    "география России",
    "жители городов",
    "история России",
    "литература",
    "наука и техника",
]

# Маркер темы «творческая личность»: для такого выпуска автоматически выбирается
# одна конкретная узнаваемая личность из списка ниже (с ротацией).
PERSONALITY_TOPIC = "творческая личность"

# Встроенный список узнаваемых деятелей искусства и культуры для отдельных
# тематических выпусков (одна личность на выпуск). Список можно переопределить
# переменной NEW_TESTS_PERSONALITIES.
DEFAULT_ARTIST_PERSONALITIES = [
    "Фёдор Шаляпин",
    "Михаил Врубель",
    "Илья Репин",
    "Иван Айвазовский",
    "Николай Рерих",
    "Исаак Левитан",
    "Валентин Серов",
    "Пётр Чайковский",
    "Сергей Рахманинов",
    "Сергей Прокофьев",
    "Дмитрий Шостакович",
    "Антон Чехов",
    "Александр Пушкин",
    "Лев Толстой",
    "Фёдор Достоевский",
    "Михаил Булгаков",
    "Марина Цветаева",
    "Анна Ахматова",
    "Сергей Есенин",
    "Владимир Маяковский",
]

# Сколько вопросов о творческих личностях подмешивать в общий тест эрудиции.
DEFAULT_PERSONALITY_MIX_COUNT = 1

# Порог по умолчанию для потока новых тестов: 15 вопросов, порог 10/15
# (формат ЗУМ/БУМ, подтверждён исследованием Дзена §9).
DEFAULT_NEW_TESTS_PASS_SCORES = [10]

# Источники изображений по умолчанию (порядок важен).
DEFAULT_PHOTO_SOURCES = ["wikimedia", "openverse"]

# Разрешённые источники вопросов для рубрик (эксперимент с рубриками).
KNOWN_RUBRIC_SOURCES = ("photo", "trap")

# Порог «провала» для репака рубрики-ловушки: доля ошибшихся (`wrong_pct`), при
# которой публикация считается провальной и её можно переупаковать (репак) с тем
# же содержанием, но новым заголовком. Переопределяется RUBRIC_REPACK_WRONG_PCT.
DEFAULT_RUBRIC_REPACK_WRONG_PCT = 60
# Минимум ответов формы, при котором метрика доли ошибшихся считается
# достаточной для решения о репаке (иначе выборка мала — репак не делаем).
DEFAULT_RUBRIC_REPACK_MIN_ANSWERS = 10
# Порог вердикта «ниже порога» в rubric_metrics.py: по медиане ЧИСЛА ОТВЕТОВ
# формы за 7 дней (прокси просмотров — просмотров API Яндекс Форм не отдаёт).
# Переопределяется RUBRIC_MIN_MEDIAN.
DEFAULT_RUBRIC_MIN_MEDIAN = 50

# Встроенный реестр рубрик эксперимента (используется, если RUBRICS не задан).
# title_templates — шаблоны заголовков с плейсхолдерами: {series_no} — номер
# выпуска серии, {threshold} — порог, {count} — число вопросов, {n} — то же,
# что {count}. Рубрики переопределяются переменной окружения RUBRICS (JSON).
DEFAULT_RUBRICS: List[Dict[str, Any]] = [
    {
        "id": "a",
        "series_name": "Знаток по кино",
        "source": "photo",
        "questions_count": 8,
        "threshold": 6,
        "comment_prompt": (
            "А теперь расскажите в комментариях: какой ваш любимый советский "
            "фильм и почему именно он?"
        ),
        "title_templates": [
            "Тест. Узнаёте эти советские фильмы по кадрам? "
            "{threshold} из {count} — вы знаток кино {series_no}",
            "Вы настоящий знаток советского кино, если угадаете "
            "{threshold} из {count} фильмов по кадру. Выпуск {series_no}",
            "Кадры из советских фильмов: соберёте {threshold} из {count}? "
            "Тест {series_no}",
        ],
        "enabled": True,
        "slot": "morning",
    },
    {
        "id": "b",
        "series_name": "Знаток по фото",
        "source": "photo",
        "questions_count": 10,
        "threshold": 8,
        # Шаблоны называют категорию («картин», «пород»), поэтому темы
        # зафиксированы: заголовок обязан соответствовать содержанию.
        "themes": ["картины из школьной программы", "породы собак"],
        "comment_prompt": (
            "Напишите в комментариях, кого или что вы угадали легче всего — "
            "так станет понятно, какие темы подбираем лучше всего."
        ),
        "title_templates": [
            "Вы настоящий искусствовед, если угадаете {threshold}/{count} "
            "картин по фото. Тест {series_no}",
            "Вы настоящий кинолог, если угадаете {threshold}/{count} "
            "пород по фото. Проверьте себя {series_no}",
            "Вы настоящий знаток картин, если угадаете {threshold}/{count} "
            "шедевров по фрагменту. Выпуск {series_no}",
        ],
        "enabled": True,
        "slot": "evening",
    },
    {
        "id": "c",
        "series_name": "Ловушка",
        "source": "trap",
        "questions_count": 10,
        "threshold": 8,
        "comment_prompt": (
            "Вопрос в конце: на каком вопросе вы попались в ловушку? "
            "Расскажите в комментариях."
        ),
        "title_templates": [
            "Тест. {wrong_pct}% прошедших ошибаются в этих вопросах. "
            "Сможете взять {threshold} из {count}? {series_no}",
            "Меньше половины прошедших этот тест не ошибаются. "
            "А вы возьмёте {threshold} из {count}? Выпуск {series_no}",
            "Даже эрудиты спотыкаются о эти вопросы: соберёте "
            "{threshold} из {count}? Тест {series_no}",
        ],
        "enabled": True,
        "slot": "afternoon",
    },
]

KNOWN_PHOTO_SOURCES = (
    "wikimedia",
    "openverse",
    "ruwiki_film",
    "filmgrab",
    "movscreencaps",
)


@dataclass
class Rubric:
    """Рубрика теста для эксперимента: серия выпусков с одним форматом."""

    id: str
    title_templates: List[str]
    series_name: str
    questions_count: int
    threshold: int
    source: str
    comment_prompt: str
    enabled: bool
    slot: str
    # Темы фото-рубрики: выпуски берутся только отсюда. Пусто — весь список
    # PHOTO_THEMES (тогда шаблоны заголовков не должны называть категорию).
    themes: List[str] = field(default_factory=list)

    def validate(self) -> None:
        errors = []
        if not self.id:
            errors.append("У рубрики пустой id")
        if self.source not in KNOWN_RUBRIC_SOURCES:
            errors.append(
                f"Рубрика «{self.id}»: неизвестный source «{self.source}». "
                "Допустимые: " + ", ".join(KNOWN_RUBRIC_SOURCES)
            )
        if self.questions_count < 1:
            errors.append(f"Рубрика «{self.id}»: questions_count должен быть >= 1")
        if self.threshold < 1:
            errors.append(f"Рубрика «{self.id}»: threshold должен быть >= 1")
        elif self.threshold >= self.questions_count:
            errors.append(
                f"Рубрика «{self.id}»: threshold ({self.threshold}) должен быть "
                f"меньше questions_count ({self.questions_count})"
            )
        if errors:
            raise ValueError("Ошибки конфигурации:\n  - " + "\n  - ".join(errors))

    def theme_candidates(self, all_themes: List[str]) -> List[str]:
        """Темы-кандидаты рубрики: свой список (иначе — все темы фото-потока).

        Темы, отсутствующие в `PHOTO_THEMES`, отбрасываются: выпуск по такой теме
        потом невозможно воспроизвести настройками.
        """
        if not self.themes:
            return list(all_themes)
        known = {str(t).strip().lower(): str(t).strip() for t in all_themes}
        result: List[str] = []
        for theme in self.themes:
            matched = known.get(theme.strip().lower())
            if matched and matched not in result:
                result.append(matched)
        return result


@dataclass
class Rubrics_config:
    """Реестр рубрик эксперимента (ключ словаря — id рубрики)."""

    rubrics: Dict[str, Rubric]

    def enabled_rubrics(self) -> List[Rubric]:
        return [rubric for rubric in self.rubrics.values() if rubric.enabled]

    def get(self, rubric_id: str) -> Optional[Rubric]:
        return self.rubrics.get(rubric_id)


def _rubric_from_dict(data: Any) -> Rubric:
    """Собрать Rubric из JSON-объекта с проверкой типов и значений."""
    if not isinstance(data, dict):
        raise ValueError("RUBRICS: каждая рубрика должна быть JSON-объектом")
    rubric_id = str(data.get("id", "")).strip()
    templates = data.get("title_templates", [])
    if not isinstance(templates, list):
        raise ValueError(f"RUBRICS: title_templates рубрики «{rubric_id or '?'}» "
                         "должен быть массивом строк")
    title_templates = [str(t).strip() for t in templates if str(t).strip()]
    raw_themes = data.get("themes", [])
    if not isinstance(raw_themes, list):
        raise ValueError(f"RUBRICS: themes рубрики «{rubric_id or '?'}» "
                         "должен быть массивом строк")
    themes = [str(t).strip() for t in raw_themes if str(t).strip()]
    return Rubric(
        id=rubric_id,
        title_templates=title_templates,
        series_name=str(data.get("series_name", "")).strip(),
        questions_count=int(data.get("questions_count", 0)),
        threshold=int(data.get("threshold", 0)),
        source=str(data.get("source", "")).strip(),
        comment_prompt=str(data.get("comment_prompt", "")).strip(),
        enabled=bool(data.get("enabled", True)),
        slot=str(data.get("slot", "")).strip(),
        themes=themes,
    )


def load_rubrics() -> Rubrics_config:
    """Загрузить реестр рубрик из RUBRICS (JSON) или взять встроенный по умолчанию."""
    value = os.getenv("RUBRICS")
    if value is None or value.strip() == "":
        raw_items = list(DEFAULT_RUBRICS)
    else:
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("RUBRICS должен быть JSON-массивом объектов рубрик")
        raw_items = parsed
    rubrics: Dict[str, Rubric] = {}
    for item in raw_items:
        rubric = _rubric_from_dict(item)
        rubric.validate()
        if not rubric.series_name:
            raise ValueError(
                f"Ошибки конфигурации:\n  - Рубрика «{rubric.id or '?'}»: "
                "пустой series_name"
            )
        if rubric.id in rubrics:
            raise ValueError(
                f"Ошибки конфигурации:\n  - Дублирующийся id рубрики «{rubric.id}»"
            )
        rubrics[rubric.id] = rubric
    if not rubrics:
        raise ValueError("RUBRICS не содержит ни одной рубрики")
    return Rubrics_config(rubrics=rubrics)


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
    photo_vision_enabled: bool = True
    # Шаблоны заголовков фото-тестов (переопределение встроенной библиотеки).
    photo_title_templates: Optional[Dict[str, List[str]]] = None
    # Сколько последних заголовков фото-тестов запрещено повторять.
    photo_title_history: int = 10

    # --- Вводная/заключительная и ревью (эрудиция и новые тесты) ---
    intro_enabled: bool = True
    conclusion_enabled: bool = True
    review_enabled: bool = True
    review_max_attempts: int = 1
    intro_min_paragraphs: int = 2
    intro_max_paragraphs: int = 4
    outro_min_paragraphs: int = 1
    outro_max_paragraphs: int = 3
    # Модель ревью; пусто — используется LLM_MODEL.
    review_model: str = ""
    # Включать вводную в текст анонса Telegram (перед CTA).
    announce_intro: bool = True

    # --- Поток «новых тестов» (отдельный канал) ---
    new_tests_enabled: bool = False
    # По решению заказчика — 15 вопросов с порогом 10/15.
    new_tests_count: int = 15
    new_tests_pass_scores: Optional[List[int]] = None
    new_tests_min_questions: Optional[int] = None
    new_tests_topics: Optional[List[str]] = None
    new_tests_survey_name: str = "Насколько широк ваш кругозор"
    new_tests_state_file: str = "new_state.json"
    new_tests_tg_target_channel: str = "@qa_helper_draft"
    # Отдельный токен бота для нового потока; пусто — используется TG_BOT_TOKEN.
    new_tests_tg_bot_token: str = ""
    new_tests_publish_telegram: bool = True
    new_tests_publish_vk: bool = False

    # --- Творческие личности ---
    # Список деятелей искусства для тематических выпусков «творческая личность».
    new_tests_personalities: Optional[List[str]] = None
    # Сколько вопросов о личностях подмешивать в общий тест эрудиции.
    personality_mix_count: int = DEFAULT_PERSONALITY_MIX_COUNT
    # Признак тематического выпуска об одной личности (выставляется в рантайме).
    personality_focus: bool = False

    # --- Факт-чекинг вопросов ---
    factcheck_enabled: bool = True
    # Модель факт-чека; пусто — используется модель ревью / LLM_MODEL.
    factcheck_model: str = ""

    # --- Эксперимент с рубриками ---
    # Реестр рубрик (RUBRICS или встроенный DEFAULT_RUBRICS); None — не загружен.
    rubrics: Optional[Rubrics_config] = None
    # Отдельный state-файл рубрик (не смешивается с фото/эрудицией).
    rubrics_state_file: str = "rubrics_state.json"
    # Порог доли ошибшихся (`wrong_pct` >= значение) для репака провальной
    # публикации рубрики-ловушки.
    rubric_repack_wrong_pct: int = DEFAULT_RUBRIC_REPACK_WRONG_PCT
    # Минимум ответов формы, при котором метрика достаточна для решения о репаке.
    rubric_repack_min_answers: int = DEFAULT_RUBRIC_REPACK_MIN_ANSWERS
    # Порог вердикта «ниже порога» в rubric_metrics.py: медиана ЧИСЛА ОТВЕТОВ
    # (прокси просмотров) за 7 дней не должна быть ниже этого значения.
    rubric_min_median: int = DEFAULT_RUBRIC_MIN_MEDIAN

    def validate(self, require_questions: bool = True) -> None:
        if self.mode == "photo":
            self._validate_photo(require_questions)
            return
        if self.mode == "new_tests":
            self._validate_new_tests(require_questions)
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

    def _validate_new_tests(self, require_questions: bool = True) -> None:
        errors = []
        if not self.dry_run:
            if not self.yandex_token:
                errors.append("YANDEX_FORMS_TOKEN не задан")
            if not self.yandex_org_id:
                errors.append("YANDEX_ORG_ID (X-Org-Id / X-Cloud-Org-Id) не задан")
        if require_questions and not self.questions_file and not self.llm_api_key:
            errors.append(
                "Для потока новых тестов нужен LLM_API_KEY "
                "(либо QUESTIONS_FILE с готовыми вопросами)"
            )
        if require_questions and self.new_tests_count < 1:
            errors.append("NEW_TESTS_COUNT должен быть >= 1")
        if errors:
            raise ValueError("Ошибки конфигурации:\n  - " + "\n  - ".join(errors))

    def effective_photo_min_questions(self) -> int:
        if self.photo_min_questions is None:
            return self.photo_questions_count
        return self.photo_min_questions

    def effective_new_tests_topics(self) -> List[str]:
        if self.new_tests_topics is not None:
            return list(self.new_tests_topics)
        return list(DEFAULT_NEW_TESTS_TOPICS)

    def effective_new_tests_min_questions(self) -> int:
        if self.new_tests_min_questions is None:
            return self.new_tests_count
        return self.new_tests_min_questions

    def effective_new_tests_pass_scores(self) -> List[int]:
        if self.new_tests_pass_scores:
            return list(self.new_tests_pass_scores)
        return list(DEFAULT_NEW_TESTS_PASS_SCORES)

    def effective_new_tests_personalities(self) -> List[str]:
        if self.new_tests_personalities:
            return list(self.new_tests_personalities)
        return list(DEFAULT_ARTIST_PERSONALITIES)

    def effective_review_model(self) -> str:
        return (self.review_model or "").strip() or self.llm_model

    def effective_factcheck_model(self) -> str:
        return (
            (self.factcheck_model or "").strip()
            or (self.review_model or "").strip()
            or self.llm_model
        )


def load_config(
    overrides: Optional[Dict[str, Any]] = None, require_questions: bool = True
) -> Config:
    """Собрать конфигурацию из окружения и применить переопределения (CLI)."""
    cfg = Config(
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip(),
        llm_temperature=_get_float("LLM_TEMPERATURE", 0.8),
        # Бюджет LLM: на каждую попытку генерации теперь приходится два вызова
        # (генерация вопросов + факт-чек), каждый — до LLM_RETRIES+1+1 запросов.
        # Худший случай при дефолтах:
        #   2 * 120 * (1+1+1) * 3 = 2160 с = 36 мин < 45 мин (daily.yml).
        # Дополнительный +1 — возможный fallback-запрос на внешнюю попытку.
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
        publish_vk=_get_bool("PUBLISH_VK", False),
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
        photo_vision_enabled=_get_bool("PHOTO_VISION_ENABLED", True),
        vision_timeout=_get_int("VISION_TIMEOUT", 120),
        photo_title_templates=_get_dict_of_str_lists("PHOTO_TITLE_TEMPLATES"),
        photo_title_history=_get_int("PHOTO_TITLE_HISTORY", 10) or 10,
        # --- Вводная/заключительная и ревью ---
        intro_enabled=_get_bool("INTRO_ENABLED", True),
        conclusion_enabled=_get_bool("CONCLUSION_ENABLED", True),
        review_enabled=_get_bool("REVIEW_ENABLED", True),
        review_max_attempts=max(0, _get_int("REVIEW_MAX_ATTEMPTS", 1) or 0),
        intro_min_paragraphs=_get_int("INTRO_MIN_PARAGRAPHS", 2) or 2,
        intro_max_paragraphs=_get_int("INTRO_MAX_PARAGRAPHS", 4) or 4,
        outro_min_paragraphs=_get_int("OUTRO_MIN_PARAGRAPHS", 1) or 1,
        outro_max_paragraphs=_get_int("OUTRO_MAX_PARAGRAPHS", 3) or 3,
        review_model=os.getenv("REVIEW_MODEL", "").strip(),
        announce_intro=_get_bool("ANNOUNCE_INTRO", True),
        # --- Поток новых тестов ---
        new_tests_enabled=_get_bool("NEW_TESTS_ENABLED", False),
        new_tests_count=_get_int("NEW_TESTS_COUNT", 15),
        new_tests_pass_scores=_get_int_list("NEW_TESTS_PASS_SCORES"),
        new_tests_min_questions=_get_int("NEW_TESTS_MIN_QUESTIONS", None),
        new_tests_topics=_get_str_list("NEW_TESTS_TOPICS"),
        new_tests_survey_name=os.getenv(
            "NEW_TESTS_SURVEY_NAME", "Насколько широк ваш кругозор"
        ).strip(),
        new_tests_state_file=os.getenv("NEW_TESTS_STATE_FILE", "new_state.json").strip(),
        new_tests_tg_target_channel=os.getenv(
            "NEW_TESTS_TG_TARGET_CHANNEL", "@qa_helper_draft"
        ).strip(),
        new_tests_tg_bot_token=os.getenv("NEW_TESTS_TG_BOT_TOKEN", "").strip(),
        new_tests_publish_telegram=_get_bool("NEW_TESTS_PUBLISH_TELEGRAM", True),
        new_tests_publish_vk=_get_bool("NEW_TESTS_PUBLISH_VK", False),
        new_tests_personalities=_get_str_list("NEW_TESTS_PERSONALITIES"),
        # --- Творческие личности ---
        personality_mix_count=max(0, _get_int("PERSONALITY_MIX_COUNT", DEFAULT_PERSONALITY_MIX_COUNT) or 0),
        personality_focus=False,
        # --- Факт-чекинг вопросов ---
        factcheck_enabled=_get_bool("FACTCHECK_ENABLED", True),
        factcheck_model=os.getenv("FACTCHECK_MODEL", "").strip(),
    )

    # Реестр рубрик эксперимента: RUBRICS (JSON) или встроенный список.
    cfg.rubrics = load_rubrics()
    cfg.rubrics_state_file = os.getenv("RUBRICS_STATE_FILE", "rubrics_state.json").strip()
    cfg.rubric_repack_wrong_pct = _get_int(
        "RUBRIC_REPACK_WRONG_PCT", DEFAULT_RUBRIC_REPACK_WRONG_PCT
    )
    cfg.rubric_repack_min_answers = _get_int(
        "RUBRIC_REPACK_MIN_ANSWERS", DEFAULT_RUBRIC_REPACK_MIN_ANSWERS
    )
    cfg.rubric_min_median = _get_int_safe("RUBRIC_MIN_MEDIAN", DEFAULT_RUBRIC_MIN_MEDIAN)

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
    if cfg.new_tests_min_questions is None:
        cfg.new_tests_min_questions = cfg.new_tests_count

    cfg.validate(require_questions=require_questions)
    return cfg
