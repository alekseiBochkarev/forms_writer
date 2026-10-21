"""Анализ статистики ответов и сборка теста из «трудных» вопросов.

Скрипт проходит по всем формам Яндекса, для каждого вопроса считает долю
верных ответов (по данным ручки `/surveys/{id}/answers`) и собирает новый
тест только из вопросов, где доля верных ответов ниже заданного порога.

Примеры:
    python hard_questions.py --report
    python hard_questions.py --create
    python hard_questions.py --create --max-rate 0.2 --min-answers 10 --limit 20
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from config import load_config
from yandex_forms import YandexFormsClient, build_quiz_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hard_questions")

ANSWER_PAGE_SIZE = 500
# Предпочтительный размер картинки при переносе в новую форму.
IMAGE_LINK_PREFERENCE = ("1280x", "720x", "2560x", "360x", "324x180", "400x512")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Тест из самых сложных вопросов")
    parser.add_argument(
        "--report",
        action="store_true",
        help="только анализ и отчёт, форму не создавать",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="создать черновик формы из отобранных вопросов",
    )
    parser.add_argument(
        "--max-rate",
        type=float,
        default=0.2,
        help="порог доли верных ответов (строго меньше), по умолчанию 0.2",
    )
    parser.add_argument(
        "--min-answers",
        type=int,
        default=10,
        help="минимальное число ответивших на вопрос, по умолчанию 10",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="ограничить число вопросов в новом тесте (0 — без ограничения)",
    )
    parser.add_argument("--name", help="название новой формы")
    parser.add_argument(
        "--report-file",
        default="hard_questions_report.json",
        help="куда сохранить JSON-отчёт",
    )
    return parser.parse_args()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _answered(entry: Any) -> bool:
    """Ответил ли респондент на вопрос (есть непустое значение)."""
    if not isinstance(entry, dict):
        return False
    value = entry.get("value")
    if value is None:
        return False
    if isinstance(value, list):
        return any(str(v).strip() for v in value)
    return bool(str(value).strip())


def _is_correct(entry: Dict[str, Any], correct_labels: set) -> bool:
    """Верен ли ответ: сперва по флагу `correct`, иначе по совпадению варианта."""
    if "correct" in entry:
        return bool(entry["correct"])
    value = entry.get("value")
    values = value if isinstance(value, list) else [value]
    return any(str(v) in correct_labels for v in values if v is not None)


def _question_meta(
    questions: List[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Карта id вопроса -> метаданные (варианты, верные ответы, картинка)."""
    meta: Dict[int, Dict[str, Any]] = {}
    for q in questions:
        if q.get("type") == "comment":
            continue
        items = q.get("items") or []
        if not items:
            continue
        options = [str(item.get("label", "")) for item in items]
        correct_labels = {
            str(item.get("label", ""))
            for item in items
            if item.get("correct")
        }
        correct_index = next(
            (i for i, item in enumerate(items) if item.get("correct")), 0
        )
        meta[q["id"]] = {
            "label": q.get("label", ""),
            "options": options,
            "correct_index": correct_index,
            "correct_labels": correct_labels,
            "image": q.get("image"),
        }
    return meta


def _iter_answers(client: YandexFormsClient, survey_id: str) -> Iterator[Dict[str, Any]]:
    """Постранично отдать все ответы формы.

    К каждому ответу добавляется служебное поле `_columns` — список id вопросов
    в том же порядке, что и элементы `data` ответа.
    """
    url: Optional[str] = f"{client.base_url}/surveys/{survey_id}/answers"
    params: Optional[Dict[str, Any]] = {"page_size": ANSWER_PAGE_SIZE}
    while url:
        response = client.session.get(url, params=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        column_ids = [col.get("id") for col in data.get("columns") or []]
        for answer in data.get("answers") or []:
            answer["_columns"] = column_ids
            yield answer
        next_url = (data.get("next") or {}).get("next_url")
        if next_url and not next_url.startswith("http"):
            # API отдаёт next_url с префиксом /v3/, но ручка доступна на /v1/.
            next_url = "https://api.forms.yandex.net" + next_url.replace("/v3/", "/v1/", 1)
        url = next_url
        params = None


def analyze_survey(
    client: YandexFormsClient, survey: Dict[str, Any], min_answers: int
) -> List[Dict[str, Any]]:
    survey_id = survey["id"]
    questions = client.get_questions(survey_id)
    meta = _question_meta(questions)
    if not meta:
        return []

    stats: Dict[int, List[int]] = {qid: [0, 0] for qid in meta}  # [answered, correct]

    for answer in _iter_answers(client, survey_id):
        data = answer.get("data") or []
        for idx, entry in enumerate(data):
            if not _answered(entry):
                continue
            qid = _column_id(answer, idx)
            if qid is None or qid not in meta:
                continue
            stats[qid][0] += 1
            if _is_correct(entry, meta[qid]["correct_labels"]):
                stats[qid][1] += 1

    records: List[Dict[str, Any]] = []
    for qid, (answered, correct) in stats.items():
        if answered < min_answers:
            continue
        rate = correct / answered if answered else 0.0
        info = meta[qid]
        records.append(
            {
                "survey_id": survey_id,
                "survey_name": survey.get("name", ""),
                "question": info["label"],
                "options": info["options"],
                "correct_index": info["correct_index"],
                "correct_answer": info["options"][info["correct_index"]],
                "image": info["image"],
                "answered": answered,
                "correct": correct,
                "rate": rate,
            }
        )
    return records


def _column_id(answer: Dict[str, Any], idx: int) -> Optional[int]:
    columns = answer.get("_columns")
    if columns and idx < len(columns):
        return columns[idx]
    return None


def collect_candidates(
    client: YandexFormsClient, min_answers: int, max_rate: float, limit: int
) -> List[Dict[str, Any]]:
    surveys = client.list_surveys()
    log.info("Форм для анализа: %s", len(surveys))
    candidates: List[Dict[str, Any]] = []
    for i, survey in enumerate(surveys, 1):
        try:
            records = analyze_survey(client, survey, min_answers)
        except Exception as exc:  # noqa: BLE001
            log.warning("Форма %s: не удалось проанализировать (%s)", survey["id"], exc)
            continue
        hard = [r for r in records if r["rate"] < max_rate]
        candidates.extend(hard)
        log.info(
            "[%s/%s] %s — вопросов с >= %s ответов: %s, из них трудных: %s",
            i,
            len(surveys),
            survey.get("name", ""),
            min_answers,
            len(records),
            len(hard),
        )

    candidates.sort(key=lambda r: (r["rate"], -r["answered"]))

    # Дедупликация по формулировке и правильному ответу.
    unique: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for rec in candidates:
        key = (_norm(rec["question"]), _norm(rec["correct_answer"]))
        if key not in unique:
            unique[key] = rec
    result = list(unique.values())
    if limit and len(result) > limit:
        result = result[:limit]
    return result


def _pick_image_link(image: Dict[str, Any]) -> Optional[str]:
    links = (image or {}).get("links") or {}
    for size in IMAGE_LINK_PREFERENCE:
        if links.get(size):
            return links[size]
    for value in links.values():
        if value:
            return value
    return None


def _download_image(url: str) -> Tuple[bytes, str, str]:
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
    ext = ".jpg"
    if "png" in content_type:
        ext = ".png"
    elif "webp" in content_type:
        ext = ".webp"
    return response.content, content_type, f"question{ext}"


def create_draft_form(
    cfg, client: YandexFormsClient, records: List[Dict[str, Any]], name: str
) -> str:
    survey_id = client.create_survey(name)
    log.info("Создана форма «%s»: %s", name, survey_id)

    client.add_comment_question(
        survey_id,
        "Эти вопросы оказались самыми сложными: с ними справились менее "
        f"{int(cfg.hard_max_rate * 100)}% участников. Проверьте себя!",
        header=False,
    )

    for i, rec in enumerate(records, 1):
        image_payload = None
        link = _pick_image_link(rec.get("image"))
        if link:
            try:
                data, content_type, filename = _download_image(link)
                image_payload = client.upload_image(
                    survey_id, data, filename, content_type
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("  вопрос %s: не удалось перенести картинку (%s)", i, exc)
        qid = client.add_enum_question(
            survey_id,
            question=rec["question"],
            options=rec["options"],
            correct_index=rec["correct_index"],
            shuffle=cfg.shuffle,
            image=image_payload,
        )
        log.info(
            "  + %2d/%s (верных %.0f%% из %s) id=%s",
            i,
            len(records),
            rec["rate"] * 100,
            rec["answered"],
            qid,
        )

    quiz = build_quiz_settings(cfg, total=len(records))
    client.update_survey(
        survey_id, {"quiz": quiz, "stats": cfg.stats, "need_auth": False}
    )
    client.set_access(survey_id, access="public", action="submit")
    log.info("Черновик готов (не опубликован): %s", YandexFormsClient.public_url(survey_id))
    return survey_id


def main() -> int:
    args = parse_args()
    if not args.report and not args.create:
        args.report = True
    if args.create and args.report:
        # --create подразумевает и отчёт; совместное указание допустимо.
        pass

    cfg = load_config({"dry_run": True}, require_questions=False)
    cfg.hard_max_rate = args.max_rate
    client = YandexFormsClient(cfg.yandex_token, cfg.yandex_org_id, cfg.yandex_org_header)

    candidates = collect_candidates(
        client, min_answers=args.min_answers, max_rate=args.max_rate, limit=args.limit
    )

    with open(args.report_file, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)
    log.info("Кандидатов (верных < %.0f%%, ответов >= %s): %s",
             args.max_rate * 100, args.min_answers, len(candidates))
    log.info("Отчёт сохранён: %s", args.report_file)

    for rec in candidates:
        log.info(
            "  %.1f%% (%s/%s) | %s -> %s",
            rec["rate"] * 100,
            rec["correct"],
            rec["answered"],
            rec["question"],
            rec["correct_answer"],
        )

    if not args.create:
        return 0

    if not candidates:
        log.error("Нет вопросов, подходящих под порог — форма не создаётся")
        return 1

    name = args.name or (
        f"Самые сложные вопросы: верных ответов меньше {int(args.max_rate * 100)}%"
    )
    survey_id = create_draft_form(cfg, client, candidates, name)
    log.info("ID формы: %s", survey_id)
    log.info("Ссылка (после публикации): %s", YandexFormsClient.public_url(survey_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())