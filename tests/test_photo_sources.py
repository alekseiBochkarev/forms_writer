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


def test_search_images_uses_explicit_sources_order(monkeypatch):
    """Переданный sources задаёт порядок обхода профильных источников."""
    calls = []

    def make(source):
        def func(cfg, query, limit):
            calls.append(source)
            return [_candidate(url=f"https://example.invalid/{source}.jpg")]
        return func

    monkeypatch.setattr(
        photo_sources,
        "SOURCE_FUNCS",
        {"filmgrab": make("filmgrab"), "movscreencaps": make("movscreencaps")},
    )

    cfg = FakePhotoCfg(photo_sources=["filmgrab", "movscreencaps"])
    search_images(cfg, "The Matrix", limit=3, sources=["movscreencaps", "filmgrab"])

    assert calls == ["movscreencaps", "filmgrab"]


def test_search_images_explicit_sources_filtered_by_enabled(monkeypatch):
    """Источник из sources, но выключенный, не вызывается."""
    calls = []

    def spy(cfg, query, limit):
        calls.append(query)
        return [_candidate()]

    monkeypatch.setattr(photo_sources, "SOURCE_FUNCS", {"filmgrab": spy})

    cfg = FakePhotoCfg(photo_sources=["wikimedia"])
    result = search_images(cfg, "кот", limit=3, sources=["filmgrab"])

    assert result == []
    assert calls == []


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


def test_search_wikimedia_skips_non_images(monkeypatch):
    """В namespace 6 не-изображения (например .pdf) отсеиваются до imageinfo."""
    search_resp = FakeResponse(
        json_data={
            "query": {
                "search": [
                    {"title": "File:A.pdf"},
                    {"title": "File:B.jpg"},
                ]
            }
        }
    )
    info_resp = FakeResponse(
        json_data={
            "query": {
                "pages": {
                    "1": {
                        "title": "File:B.jpg",
                        "imageinfo": [
                            {
                                "thumburl": "https://upload.example/b.jpg",
                                "descriptionurl": "https://commons.example/b",
                                "mime": "image/jpeg",
                                "extmetadata": {},
                            }
                        ],
                    }
                }
            }
        }
    )
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append(params)
        return search_resp if len(calls) == 1 else info_resp

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    cfg = FakePhotoCfg()

    result = search_wikimedia(cfg, "пример", limit=5)

    assert len(result) == 1
    assert calls[1]["titles"] == "File:B.jpg"


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


def test_search_ruwiki_film_parses_file_search(monkeypatch):
    """ruwiki_film: поиск в файловом namespace -> imageinfo, не-картинки отсеяны."""
    search_resp = FakeResponse(
        json_data={
            "query": {
                "search": [
                    {"title": "File:Иван Васильевич.jpg"},
                    {"title": "File:Съёмки.pdf"},
                ]
            }
        }
    )
    info_resp = FakeResponse(
        json_data={
            "query": {
                "pages": {
                    "1": {
                        "title": "File:Иван Васильевич.jpg",
                        "imageinfo": [
                            {
                                "thumburl": "https://upload.example/ivan.jpg",
                                "descriptionurl": "https://ru.wikipedia.org/wiki/File:Иван",
                                "mime": "image/jpeg",
                                "width": 1920,
                                "height": 1080,
                                "extmetadata": {
                                    "Artist": {"value": "Мосфильм"},
                                    "LicenseShortName": {"value": "PD"},
                                },
                            }
                        ],
                    }
                }
            }
        }
    )
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append(params)
        return search_resp if len(calls) == 1 else info_resp

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    cfg = FakePhotoCfg(film_ru_enabled=True)

    result = photo_sources.search_ruwiki_film(cfg, "Иван Васильевич", 5)

    assert len(result) == 1
    assert result[0].source == "ruwiki_film"
    assert result[0].url == "https://upload.example/ivan.jpg"
    assert result[0].author == "Мосфильм"
    # первый запрос — файловый namespace, поиск по названию фильма
    assert calls[0]["list"] == "search"
    assert calls[0]["srnamespace"] == 6
    assert calls[0]["srsearch"] == "Иван Васильевич"
    # во второй запрос pdf не попал
    assert calls[1]["titles"] == "File:Иван Васильевич.jpg"


def test_search_ruwiki_film_prefers_frames_over_posters(monkeypatch):
    """Кадры сортируются вперёд, постеры — в конец, без отбрасывания."""
    search_resp = FakeResponse(
        json_data={
            "query": {
                "search": [
                    {"title": "File:Фильм постер.jpg"},
                    {"title": "File:Фильм.jpg"},
                    {"title": "File:Фильм кадр.jpg"},
                ]
            }
        }
    )
    info_resp = FakeResponse(json_data={"query": {"pages": {}}})
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append(params)
        return search_resp if len(calls) == 1 else info_resp

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    cfg = FakePhotoCfg(film_ru_enabled=True)

    photo_sources.search_ruwiki_film(cfg, "Фильм", 5)

    assert calls[1]["titles"] == (
        "File:Фильм кадр.jpg|File:Фильм.jpg|File:Фильм постер.jpg"
    )


def test_search_filmgrab_prefers_slug_matching_query(monkeypatch):
    """При выборе страницы предпочитается slug, совпавший с названием фильма."""
    search_html = (
        '<a href="https://film-grab.com/2010/other-movie/">other</a>'
        '<a href="https://film-grab.com/2012/the-matrix/">matrix</a>'
    )
    page_html = '<img src="/wp-content/uploads/the-matrix-01.jpg">'

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        return FakeResponse(text=page_html)

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(film_grab_enabled=True)
    result = photo_sources.search_filmgrab(cfg, "The Matrix", limit=5)

    assert len(result) == 1
    assert result[0].page_url == "https://film-grab.com/2012/the-matrix/"
    assert result[0].url == "https://film-grab.com/wp-content/uploads/the-matrix-01.jpg"


def test_search_moviescreencaps_prefers_slug_matching_query(monkeypatch):
    """movie-screencaps: год внутри слага — страница всё равно выбирается.

    Путь вида `/the-matrix-1999-4k/` не содержит `/20xx/`, поэтому старый
    отбор по году источник ломал.
    """
    search_html = (
        '<a href="https://movie-screencaps.com/the-matrix-1999-4k/">matrix</a>'
        '<a href="https://movie-screencaps.com/other-movie-2001/">other</a>'
    )
    page_html = '<img src="/wp-content/uploads/the-matrix-1999-4k-01.jpg">'

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        return FakeResponse(text=page_html)

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(movie_screencaps_enabled=True)
    result = photo_sources.search_moviescreencaps(cfg, "The Matrix", limit=5)

    assert len(result) == 1
    assert result[0].page_url == "https://movie-screencaps.com/the-matrix-1999-4k/"
    assert result[0].url == (
        "https://movie-screencaps.com/wp-content/uploads/the-matrix-1999-4k-01.jpg"
    )


def test_search_filmgrab_ignores_foreign_host(monkeypatch):
    """Похожий хост (film-grab.com.evil.tld) не принимается за целевой."""
    search_html = (
        '<a href="https://film-grab.com.evil.tld/2012/the-matrix/">evil</a>'
    )

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        raise AssertionError("страница чужого хоста не должна запрашиваться")

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(film_grab_enabled=True)
    assert photo_sources.search_filmgrab(cfg, "The Matrix", limit=5) == []


def test_search_filmgrab_short_title_uses_first_film_page(monkeypatch):
    """Короткое название («Up»): навигация идёт первой, но берётся страница фильма.

    Раньше fallback цеплял первую неслужебную ссылку, поэтому nav-страницы
    (`/browse-by-artist/`, `/xmlrpc.php`) уводили поиск не туда.
    """
    search_html = (
        '<a href="https://film-grab.com/browse-by-artist/">artists</a>'
        '<a href="https://film-grab.com/contact/">contact</a>'
        '<a href="https://film-grab.com/xmlrpc.php">rpc</a>'
        '<a href="https://film-grab.com/movies-a-z/">a-z</a>'
        # Ассет с годом в пути не должен приниматься за страницу фильма.
        '<a href="https://film-grab.com/wp-content/uploads/2019/02/icon.jpg">icon</a>'
        '<a href="https://film-grab.com/2009/up/">up</a>'
    )
    page_html = '<img src="/wp-content/uploads/up-01.jpg">'

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        return FakeResponse(text=page_html)

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(film_grab_enabled=True)
    result = photo_sources.search_filmgrab(cfg, "Up", limit=5)

    assert len(result) == 1
    assert result[0].page_url == "https://film-grab.com/2009/up/"


def test_search_filmgrab_contact_prefers_movie_over_nav(monkeypatch):
    """«Contact»: раздел `/contact/` без года не должен опережать фильм с годом."""
    search_html = (
        '<a href="https://film-grab.com/contact/">contact nav</a>'
        '<a href="https://film-grab.com/2019/contact/">contact movie</a>'
    )
    page_html = '<img src="/wp-content/uploads/contact-01.jpg">'

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        return FakeResponse(text=page_html)

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(film_grab_enabled=True)
    result = photo_sources.search_filmgrab(cfg, "Contact", limit=5)

    assert len(result) == 1
    assert result[0].page_url == "https://film-grab.com/2019/contact/"


def test_search_filmgrab_nav_only_returns_empty(monkeypatch):
    """Если страниц фильма нет вовсе — возвращается [], а не навигация."""
    search_html = (
        '<a href="https://film-grab.com/browse-by-artist/">artists</a>'
        '<a href="https://film-grab.com/contact/">contact</a>'
        '<a href="https://film-grab.com/xmlrpc.php">rpc</a>'
        '<a href="https://film-grab.com/movies-a-z/">a-z</a>'
    )

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        raise AssertionError("навигационная страница не должна запрашиваться")

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(film_grab_enabled=True)
    assert photo_sources.search_filmgrab(cfg, "Up", limit=5) == []


def test_search_moviescreencaps_prefers_exact_slug_over_first_page(monkeypatch):
    """Короткое название «Up»: чужая страница со словом «up» не должна побеждать.

    `upgrade-2018` идёт первой, но хвост года у `up-2009-4k` срезается, и
    точное совпадение slug (`up`) имеет приоритет.
    """
    search_html = (
        '<a href="https://movie-screencaps.com/upgrade-2018/">upgrade</a>'
        '<a href="https://movie-screencaps.com/up-2009-4k/">up</a>'
    )
    page_html = '<img src="/wp-content/uploads/up-2009-4k-01.jpg">'

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        return FakeResponse(text=page_html)

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(movie_screencaps_enabled=True)
    result = photo_sources.search_moviescreencaps(cfg, "Up", limit=5)

    assert len(result) == 1
    assert result[0].page_url == "https://movie-screencaps.com/up-2009-4k/"


def test_search_moviescreencaps_ignores_nav_before_film(monkeypatch):
    """movie-screencaps: `/xmlrpc.php` до результата не должен становиться страницей."""
    search_html = (
        '<a href="https://movie-screencaps.com/xmlrpc.php">rpc</a>'
        '<a href="https://movie-screencaps.com/the-matrix-1999-4k/">matrix</a>'
    )
    page_html = '<img src="/wp-content/uploads/the-matrix-1999-4k-01.jpg">'

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        return FakeResponse(text=page_html)

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(movie_screencaps_enabled=True)
    result = photo_sources.search_moviescreencaps(cfg, "The Matrix", limit=5)

    assert len(result) == 1
    assert result[0].page_url == "https://movie-screencaps.com/the-matrix-1999-4k/"


@pytest.mark.parametrize(
    "query, movie_path",
    [
        ("About a Boy", "2010/about-a-boy/"),
        ("Contact", "2019/contact/"),
        ("Tag", "2018/tag/"),
        ("Tag 2018", "2018/tag-2018/"),
    ],
)
def test_search_filmgrab_keeps_movies_with_service_words(monkeypatch, query, movie_path):
    """Служебное слово лишь часть пути фильма — страница не отбрасывается."""
    search_html = (
        '<a href="https://film-grab.com/movies-a-z/">a-z</a>'
        '<a href="https://film-grab.com/about/">about</a>'
        '<a href="https://film-grab.com/2010/other-movie/?page=2">page</a>'
        f'<a href="https://film-grab.com/{movie_path}">movie</a>'
    )
    page_html = '<img src="/wp-content/uploads/frame-01.jpg">'

    def fake_get(url, **kwargs):
        if "robots.txt" in str(url):
            return FakeResponse(status_code=404)
        if "?s=" in str(url):
            return FakeResponse(text=search_html)
        return FakeResponse(text=page_html)

    monkeypatch.setattr(photo_sources.requests, "get", fake_get)
    monkeypatch.setattr(photo_sources.time, "sleep", lambda *_: None)

    cfg = FakePhotoCfg(film_grab_enabled=True)
    result = photo_sources.search_filmgrab(cfg, query, limit=5)

    assert len(result) == 1
    assert result[0].page_url == f"https://film-grab.com/{movie_path}"