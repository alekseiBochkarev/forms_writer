"""Общие тестовые хелперы: фейковые HTTP-ответы и заглушка конфига фото-потока.

Все хелперы не ходят в сеть — это важно, тесты должны быть полностью
изолированными (никаких реальных LLM/Яндекс Форм/Wikimedia/TG/VK).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import requests


class FakeResponse:
    """Минимальная замена `requests.Response` для не-стриминговых запросов."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Optional[Any] = None,
        text: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        if text is not None:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data, ensure_ascii=False)
        else:
            self.text = ""
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("В ответе нет JSON")
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}", response=self
            )


class FakeStreamResponse(FakeResponse):
    """Замена `requests.Response` с поддержкой контекстного менеджера/потока."""

    def __init__(
        self,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        chunks: Optional[List[bytes]] = None,
    ) -> None:
        super().__init__(status_code=status_code, headers=headers)
        self._chunks = list(chunks or [])

    def __enter__(self) -> "FakeStreamResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def iter_content(self, chunk_size: int = 1):
        for chunk in self._chunks:
            yield chunk


class FakePhotoCfg:
    """Заглушка `config.Config` со всеми полями, которые трогает фото-поток.

    Позволяет тестировать photo_flow/photo_sources/vision без построения полного
    датакласса Config (у которого много обязательных полей).
    """

    def __init__(self, **overrides: Any) -> None:
        self.photo_flow_enabled = False
        self.photo_theme: Optional[str] = None
        self.photo_themes: Optional[List[str]] = None
        self.photo_sources: Optional[List[str]] = ["wikimedia"]
        self.photo_questions_count = 5
        self.photo_min_questions: Optional[int] = 5
        self.photo_max_image_attempts = 3
        self.photo_image_timeout = 30
        self.photo_image_max_bytes = 8_000_000
        self.photo_state_file = "photo_state.json"
        self.photo_survey_name = "Что на фото"
        self.shuffle = True
        self.publish = False
        self.number_surveys = False
        self.stats = True
        self.show_results = True
        self.show_correct = True
        self.segments = None
        self.pass_scores = None
        self.dry_run = True
        self.force = False
        self.yandex_token = ""
        self.yandex_org_id = ""
        self.yandex_org_header = "X-Cloud-Org-Id"
        self.llm_api_key = "test-key"
        self.llm_base_url = "https://example.invalid/v1"
        self.llm_model = "test-model"
        self.llm_temperature = 0.0
        self.wikimedia_user_agent = ""
        self.photo_vision_enabled = False
        self.vision_api_key = ""
        self.vision_base_url = "https://example.invalid/v1"
        self.vision_model = "test-model"
        self.vision_timeout = 10
        self.openverse_api_key = ""
        self.openverse_base_url = "https://example.invalid/v1"
        self.film_ru_enabled = False
        self.film_grab_enabled = False
        self.movie_screencaps_enabled = False
        self.__dict__.update(overrides)

    def enabled_photo_sources(self) -> List[str]:
        return list(self.photo_sources or ["wikimedia"])

    def effective_photo_themes(self) -> List[str]:
        if self.photo_themes is not None:
            return list(self.photo_themes)
        return ["животные", "растения", "актёры"]

    def effective_photo_min_questions(self) -> int:
        if self.photo_min_questions is None:
            return self.photo_questions_count
        return self.photo_min_questions