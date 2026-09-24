"""Источники изображений для фото-тестов.

Каждый источник возвращает список нормализованных кандидатов `ImageCandidate`.
Сетевые ошибки логируются, но не прерывают общий поиск: источники ненадёжны,
поэтому при недоступности одного из них используются остальные.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import List
from urllib.parse import quote_plus, urljoin

import requests

from config import DEFAULT_PHOTO_SOURCES

log = logging.getLogger(__name__)

WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
RUWIKI_API = "https://ru.wikipedia.org/w/api.php"

# Категория кадров из советских фильмов на ru.wikipedia.
# Точное имя может потребовать уточнения — при смене категории поиск вернёт пусто.
RUWIKI_FILM_CATEGORY = "Категория:Кадры из фильмов СССР"

FILMGRAB_BASE = "https://film-grab.com"
FILMGRAB_SEARCH = "https://film-grab.com/?s={query}"
MOVIECAPS_BASE = "https://movie-screencaps.com"
MOVIECAPS_SEARCH = "https://movie-screencaps.com/?s={query}"

# Реалистичный User-Agent для HTML-источников (Wikimedia задаётся отдельно).
DEFAULT_USER_AGENT = (
    "forms_writer/1.0 (photo quiz generator; +https://github.com/alekseiBochkarev/forms_writer)"
)

IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?|$)", re.IGNORECASE)
IMG_TAG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
LINK_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class ImageCandidate:
    source: str       # wikimedia | openverse | ruwiki_film | filmgrab | movscreencaps
    url: str          # прямая ссылка на изображение
    page_url: str
    title: str
    author: str
    license: str
    mime: str
    width: int = 0
    height: int = 0


def _clean_html(value: object) -> str:
    if not value:
        return ""
    return TAG_RE.sub("", str(value)).strip()


def _meta(extmetadata: dict, key: str) -> str:
    """Достать значение из extmetadata (там {key: {"value": ...}})."""
    item = (extmetadata or {}).get(key) or {}
    if isinstance(item, dict):
        return _clean_html(item.get("value"))
    return _clean_html(item)


def _get_imageinfo(cfg, api_url: str, titles: List[str]) -> List[dict]:
    """Запросить imageinfo по списку файлов (до 50 за раз)."""
    if not titles:
        return []
    headers = {"User-Agent": cfg.wikimedia_user_agent or DEFAULT_USER_AGENT}
    params = {
        "action": "query",
        "format": "json",
        "titles": "|".join(titles[:50]),
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
        "iiurlwidth": 1280,
    }
    response = requests.get(api_url, params=params, headers=headers, timeout=cfg.photo_image_timeout)
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", {})
    # pages — словарь pageid -> данные (в новых версиях) или список.
    if isinstance(pages, dict):
        return list(pages.values())
    return pages


def search_wikimedia(cfg, query: str, limit: int) -> List[ImageCandidate]:
    """Найти изображения в Wikimedia Commons через Action API."""
    headers = {"User-Agent": cfg.wikimedia_user_agent or DEFAULT_USER_AGENT}
    params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": query,
        "srnamespace": 6,
        "srlimit": max(1, limit),
    }
    response = requests.get(
        WIKIMEDIA_API, params=params, headers=headers, timeout=cfg.photo_image_timeout
    )
    response.raise_for_status()
    hits = response.json().get("query", {}).get("search", [])
    titles = [h.get("title", "") for h in hits if h.get("title")]
    if not titles:
        return []

    candidates: List[ImageCandidate] = []
    for page in _get_imageinfo(cfg, WIKIMEDIA_API, titles):
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        ext = info.get("extmetadata") or {}
        candidates.append(
            ImageCandidate(
                source="wikimedia",
                url=url,
                page_url=info.get("descriptionurl", ""),
                title=page.get("title", "").replace("File:", ""),
                author=_meta(ext, "Artist"),
                license=_meta(ext, "LicenseShortName"),
                mime=info.get("mime", ""),
                width=int(info.get("thumbwidth") or info.get("width") or 0),
                height=int(info.get("thumbheight") or info.get("height") or 0),
            )
        )
    return candidates


def search_openverse(cfg, query: str, limit: int) -> List[ImageCandidate]:
    """Найти изображения в Openverse."""
    headers = {}
    if cfg.openverse_api_key:
        headers["Authorization"] = f"Bearer {cfg.openverse_api_key}"
    params = {"q": query, "page_size": max(1, limit)}
    response = requests.get(
        f"{cfg.openverse_base_url}/images/",
        params=params,
        headers=headers,
        timeout=cfg.photo_image_timeout,
    )
    response.raise_for_status()

    candidates: List[ImageCandidate] = []
    for item in response.json().get("results", []):
        url = item.get("url") or item.get("thumbnail")
        if not url:
            continue
        license_text = item.get("license") or ""
        if item.get("license_version"):
            license_text = f"{license_text} {item.get('license_version')}".strip()
        candidates.append(
            ImageCandidate(
                source="openverse",
                url=url,
                page_url=item.get("foreign_landing_url", "") or "",
                title=item.get("title", "") or "",
                author=item.get("creator", "") or "",
                license=license_text,
                mime="",
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
            )
        )
    return candidates


def search_ruwiki_film(cfg, query: str, limit: int) -> List[ImageCandidate]:
    """Найти кадры из советских фильмов в ru.wikipedia (по фича-флагу)."""
    if not cfg.film_ru_enabled:
        return []
    headers = {"User-Agent": cfg.wikimedia_user_agent or DEFAULT_USER_AGENT}
    params = {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": RUWIKI_FILM_CATEGORY,
        "cmtype": "file",
        "cmlimit": max(1, limit),
    }
    response = requests.get(
        RUWIKI_API, params=params, headers=headers, timeout=cfg.photo_image_timeout
    )
    response.raise_for_status()
    members = response.json().get("query", {}).get("categorymembers", [])
    titles = [m.get("title", "") for m in members if m.get("title")]

    # Простейшая фильтрация по запросу: оставляем файлы, где встречаются его слова.
    tokens = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
    if tokens:
        filtered = [t for t in titles if any(tok in t.lower() for tok in tokens)]
        titles = filtered or titles

    candidates: List[ImageCandidate] = []
    for page in _get_imageinfo(cfg, RUWIKI_API, titles):
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        ext = info.get("extmetadata") or {}
        candidates.append(
            ImageCandidate(
                source="ruwiki_film",
                url=url,
                page_url=info.get("descriptionurl", ""),
                title=page.get("title", "").replace("File:", ""),
                author=_meta(ext, "Artist"),
                license=_meta(ext, "LicenseShortName"),
                mime=info.get("mime", ""),
                width=int(info.get("thumbwidth") or info.get("width") or 0),
                height=int(info.get("thumbheight") or info.get("height") or 0),
            )
        )
    return candidates


def _robots_allows(cfg, base_url: str, url: str, user_agent: str) -> bool:
    """Проверить robots.txt; при любой ошибке считаем, что доступно.

    robots.txt скачивается через requests с таймаутом (в отличие от
    RobotFileParser.read(), который ходит без таймаута и может зависнуть).
    """
    try:
        response = requests.get(
            urljoin(base_url, "/robots.txt"),
            headers={"User-Agent": user_agent},
            timeout=cfg.photo_image_timeout,
        )
        if response.status_code >= 400:
            return True
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser.can_fetch(user_agent, url)
    except Exception as exc:  # noqa: BLE001 - robots.txt не критичен
        log.debug("Не удалось проверить robots.txt %s: %s", base_url, exc)
        return True


def _html_image_search(
    cfg, search_url: str, base_url: str, user_agent: str, limit: int, source: str
) -> List[ImageCandidate]:
    """Общий сценарий для HTML-источников: поиск -> страница -> прямые ссылки."""
    if not _robots_allows(cfg, base_url, search_url, user_agent):
        log.info("robots.txt запрещает запрос к %s", search_url)
        return []

    headers = {"User-Agent": user_agent}
    response = requests.get(search_url, headers=headers, timeout=cfg.photo_image_timeout)
    response.raise_for_status()
    time.sleep(0.5)  # щадящий режим, чтобы не нагружать источник

    # Ищем ссылку на страницу выпуска (не на служебные разделы).
    page_url = ""
    for href in LINK_RE.findall(response.text):
        absolute = urljoin(base_url, href)
        if absolute.startswith(base_url) and re.search(r"/20\d\d/?", absolute):
            page_url = absolute
            break
    if not page_url:
        return []

    if not _robots_allows(cfg, base_url, page_url, user_agent):
        log.info("robots.txt запрещает запрос к %s", page_url)
        return []

    response = requests.get(page_url, headers=headers, timeout=cfg.photo_image_timeout)
    response.raise_for_status()

    candidates: List[ImageCandidate] = []
    seen = set()
    for src in IMG_TAG_RE.findall(response.text):
        absolute = urljoin(base_url, src)
        if not IMAGE_EXT_RE.search(absolute) or absolute in seen:
            continue
        seen.add(absolute)
        candidates.append(
            ImageCandidate(
                source=source,
                url=absolute,
                page_url=page_url,
                title=page_url.rsplit("/", 1)[-1].replace("-", " "),
                author="",
                license="",
                mime="",
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def search_filmgrab(cfg, query: str, limit: int) -> List[ImageCandidate]:
    """Найти кадры на film-grab.com (только при FILM_GRAB_ENABLED)."""
    if not cfg.film_grab_enabled:
        return []
    return _html_image_search(
        cfg,
        FILMGRAB_SEARCH.format(query=quote_plus(query)),
        FILMGRAB_BASE,
        DEFAULT_USER_AGENT,
        limit,
        "filmgrab",
    )


def search_moviescreencaps(cfg, query: str, limit: int) -> List[ImageCandidate]:
    """Найти кадры на movie-screencaps.com (только при MOVIE_SCREENCAPS_ENABLED)."""
    if not cfg.movie_screencaps_enabled:
        return []
    return _html_image_search(
        cfg,
        MOVIECAPS_SEARCH.format(query=quote_plus(query)),
        MOVIECAPS_BASE,
        DEFAULT_USER_AGENT,
        limit,
        "movscreencaps",
    )


# Функции источников по имени (для порядка обхода в search_images).
SOURCE_FUNCS = {
    "wikimedia": search_wikimedia,
    "openverse": search_openverse,
    "ruwiki_film": search_ruwiki_film,
    "filmgrab": search_filmgrab,
    "movscreencaps": search_moviescreencaps,
}

# Порядок обхода: сначала надёжные источники, затем кино-источники.
SOURCE_ORDER = ["wikimedia", "openverse", "ruwiki_film", "filmgrab", "movscreencaps"]


def search_images(cfg, query: str, limit: int = 8) -> List[ImageCandidate]:
    """Опросить все включённые источники и собрать кандидатов.

    Ошибки каждого источника логируются, но не прерывают поиск по остальным.
    """
    enabled = set(cfg.enabled_photo_sources())
    result: List[ImageCandidate] = []
    for source in SOURCE_ORDER:
        if source not in enabled:
            continue
        func = SOURCE_FUNCS[source]
        try:
            found = func(cfg, query, limit)
        except Exception as exc:  # noqa: BLE001 - источник ненадёжен
            log.warning("Источник %s недоступен: %s", source, exc)
            continue
        if found:
            log.info("Источник %s: найдено %s изображений", source, len(found))
        result.extend(found)
    return result


def download_image(cfg, candidate: ImageCandidate) -> tuple[bytes, str]:
    """Скачать изображение с ограничением размера.

    Возвращает (данные, mime). При ошибке поднимает RuntimeError.
    """
    headers = {"User-Agent": cfg.wikimedia_user_agent or DEFAULT_USER_AGENT}
    try:
        with requests.get(
            candidate.url, headers=headers, timeout=cfg.photo_image_timeout, stream=True
        ) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type and not content_type.startswith("image/"):
                raise RuntimeError(f"Не изображение: Content-Type={content_type!r}")
            chunks: List[bytes] = []
            total = 0
            for chunk in response.iter_content(65536):
                total += len(chunk)
                if total > cfg.photo_image_max_bytes:
                    raise RuntimeError(
                        f"Изображение превышает лимит {cfg.photo_image_max_bytes} байт"
                    )
                chunks.append(chunk)
    except requests.RequestException as exc:
        log.warning("Не удалось скачать изображение %s: %s", candidate.url, exc)
        raise RuntimeError(f"Ошибка загрузки изображения: {exc}") from exc

    data = b"".join(chunks)
    if not data:
        raise RuntimeError("Пустое изображение")
    mime = content_type or candidate.mime or "image/jpeg"
    if not mime.startswith("image/"):
        raise RuntimeError(f"Не изображение: Content-Type={content_type!r}")
    return data, mime