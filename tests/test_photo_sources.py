"""Тесты photo_sources.py: скачивание, поиск и разбор ответов (сеть замокана)."""

from __future__ import annotations

import pytest
import requests

import photo_sources
from helpers import FakePhotoCfg, FakeResponse, FakeStreamResponse
from photo_sources import ImageCandidate, download_image, search_images, search_wikimedia


def _candidate(url="https://example.invalid/a.jpg", mime=""):
    return ImageCandidate(
        source="wikimedia",
        url=url,
        page_url="https://example.invalid/page",
        title="a",
        author="author",
        license="CC",
        mime=mime,
    )


# --- download_image ---------------------------------------------------------


def test_download_image_html_content_type_rejected(monkeypatch):
    """Content-Type: text/html — RuntimeError (это не изображение)."""
    response = FakeStreamResponse(
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        chunks=[b"<html></html>"],
    )
    monkeypatch.setattr(photo_sources.requests, "get", lambda *a, **k: response)

    cfg = FakePhotoCfg()
    with pytest.raises(RuntimeError) as exc:
        download_image(cfg, _candidate())

    assert "Не изображение" in str(exc.value)


def test_download_image_exceeding_max_bytes_rejected(monkeypatch):
    """Превышение photo_image_max_bytes — RuntimeError, данные не собираются."""
    response = FakeStreamResponse(
        status_code=200,
        headers={"Content-Type": "image/jpeg"},
        chunks=[b"1234567890", b"1"],
    )
    monkeypatch.setattr(photo_sources.requests, "get", lambda *a, **k: response)

    cfg = FakePhotoCfg(photo_image_max_bytes=10)
    with pytest.raises(RuntimeError) as exc:
        download_image(cfg, _candidate())

    assert "превышает лимит" in str(exc.value)


def test_download_image_valid_returns_bytes_and_mime(monkeypatch):
    """Корректный image/jpeg: возвращаются байты и mime без параметров."""
    response = FakeStreamResponse(
        status_code=200,
        headers={"Content-Type": "image/jpeg; charset=binary"},
        chunks=[b"\xff\xd8", b"\xff\xe0"],
    )
    monkeypatch.setattr(photo_sources.requests, "get", lambda *a, **k: response)

    cfg = FakePhotoCfg()
    data, mime = download_image(cfg, _candidate())

    assert data == b"\xff\xd8\xff\xe0"
    assert mime == "image/jpeg"


def test_download_image_empty_body_rejected(monkeypatch):
    """Пустое тело ответа — RuntimeError."""
    response = FakeStreamResponse(
        status_code=200, headers={"Content-Type": "image/jpeg"}, chunks=[]
    )
    monkeypatch.setattr(photo_sources.requests, "get", lambda *a, **k: response)

    cfg = FakePhotoCfg()
    with pytest.raises(RuntimeError) as exc:
        download_image(cfg, _candidate())

    assert "Пустое изображение" in str(exc.value)


def test_download_image_network_error_wrapped(monkeypatch):
    """Сетевая ошибка requests оборачивается в RuntimeError."""
    def boom(*args, **kwargs):
        raise requests.ConnectionError("нет соединения")

    monkeypatch.setattr(photo_sources.requests, "get", boom)

    cfg = FakePhotoCfg()
    with pytest.raises(RuntimeError) as exc:
        download_image(cfg, _candidate())

    assert "Ошибка загрузки изображения" in str(exc.value)


def test_download_image_http_error_wrapped(monkeypatch):
    """HTTP 500 при скачивании — RuntimeError."""
    response = FakeStreamResponse(
        status_code=500, headers={"Content-Type": "image/jpeg"}, chunks=[b"x"]
    )
    monkeypatch.setattr(photo_sources.requests, "get", lambda *a, **k: response)

    cfg = FakePhotoCfg()
    with pytest.raises(RuntimeError):
        download_image(cfg, _candidate())


# --- search_images: изоляция сбоев источников -------------------------------


def test_search_images_one_failure_does_not_break_others(monkeypatch):
    """Сбой одного источника не мешает собрать кандидатов с другого."""
    good = _candidate(url="https://example.invalid/good.jpg")
    calls = []

    def failing(cfg, query, limit):
        calls.append("wikimedia")
        raise RuntimeError("источник лежит")

    def working(cfg, query, limit):
        calls.append("openverse")
        return [good]

    monkeypatch.setattr(
        photo_sources, "SOURCE_FUNCS", {"wikimedia": failing, "openverse": working}
    )

    cfg = FakePhotoCfg(photo_sources=["wikimedia", "openverse"])
    result = search_images(cfg, "кот", limit=5)

    assert result == [good]
    assert calls == ["wikimedia", "openverse"]


def test_search_images_skips_disabled_sources(monkeypatch):
    """Выключенные источники не вызываются."""
    calls = []

    def spy(cfg, query, limit):
        calls.append(query)
        return [_candidate()]

    monkeypatch.setattr(
        photo_sources, "SOURCE_FUNCS", {"wikimedia": spy, "openverse": spy}
    )

    cfg = FakePhotoCfg(photo_sources=["wikimedia"])
    search_images(cfg, "кот", limit=5)

    assert calls == ["кот"]


# --- search_wikimedia: разбор ответов без сети ------------------------------


def test_search_wikimedia_parses_candidates(monkeypatch):
    """Action API: search + imageinfo -> нормализованный ImageCandidate."""
    search_resp = FakeResponse(
        json_data={"query": {"search": [{"title": "File:A.jpg"}]}}
    )
    info_resp = FakeResponse(
        json_data={
            "query": {
                "pages": {
                    "1": {
                        "title": "File:A.jpg",
                        "imageinfo": [
                            {
                                "thumburl": "https://upload.example/a.jpg",
                                "descriptionurl": "https://commons.example/a",
                                "mime": "image/jpeg",
                                "thumbwidth": 1280,
                                "thumbheight": 720,
                                "extmetadata": {
                                    "Artist": {"value": "<b>Иван</b>"},
                                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                },
                            }
                        ],
                    }
                }
            }
        }
    )
    responses = iter([search_resp, info_resp])

    def fake_get(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)

    cfg = FakePhotoCfg()
    result = search_wikimedia(cfg, "кот", limit=5)

    assert len(result) == 1
    candidate = result[0]
    assert candidate.source == "wikimedia"
    assert candidate.url == "https://upload.example/a.jpg"
    # HTML-теги из extmetadata вычищены
    assert candidate.author == "Иван"
    assert candidate.license == "CC BY-SA 4.0"
    assert candidate.title == "A.jpg"
    assert candidate.width == 1280


def test_search_openverse_parses_candidates(monkeypatch):
    """Openverse API: results -> ImageCandidate (url, лицензия, creator)."""
    response = FakeResponse(
        json_data={
            "results": [
                {
                    "url": "https://openverse.example/a.jpg",
                    "foreign_landing_url": "https://openverse.example/a",
                    "title": "Кот",
                    "creator": "Пётр",
                    "license": "by",
                    "license_version": "4.0",
                    "width": 800,
                    "height": 600,
                }
            ]
        }
    )
    monkeypatch.setattr(photo_sources.requests, "get", lambda *a, **k: response)

    cfg = FakePhotoCfg()
    result = photo_sources.search_openverse(cfg, "кот", limit=5)

    assert len(result) == 1
    candidate = result[0]
    assert candidate.source == "openverse"
    assert candidate.url == "https://openverse.example/a.jpg"
    assert candidate.author == "Пётр"
    assert candidate.license == "by 4.0"


def test_search_ruwiki_film_disabled_returns_empty_without_network(monkeypatch):
    """При film_ru_enabled=False запрос не делается."""
    def forbidden(*args, **kwargs):
        raise AssertionError("источник выключен, сеть недопустима")

    monkeypatch.setattr(photo_sources.requests, "get", forbidden)

    cfg = FakePhotoCfg(film_ru_enabled=False)
    assert photo_sources.search_ruwiki_film(cfg, "фильм", 5) == []