"""Проверка вопросов на повторы (в т.ч. по предыдущим выпускам)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Set

SIMILARITY_THRESHOLD = 0.82


def canonical(text: str) -> str:
    """Привести вопрос к каноническому виду для сравнения."""
    value = (text or "").lower().strip()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _similar(a: str, b: str, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


class DuplicateChecker:
    """Хранит канонические тексты вопросов и умеет проверять новые."""

    def __init__(self, history: Optional[Iterable[str]] = None):
        self._canon: List[str] = []
        self._exact: Set[str] = set()
        for text in history or []:
            self.add(text)

    def add(self, text: str) -> None:
        value = canonical(text)
        if not value:
            return
        self._canon.append(value)
        self._exact.add(value)

    def is_duplicate(self, text: str) -> bool:
        value = canonical(text)
        if not value:
            return True
        if value in self._exact:
            return True
        return any(_similar(value, existing) for existing in self._canon)