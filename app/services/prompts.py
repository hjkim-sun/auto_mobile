"""Jev 판별 프롬프트. 이벤트 종류(events.source)마다 PROMPTS_DIR/{source}.toml 하나를 쓴다.

  message.toml   SMS/MMS
  email.toml     메일

파일 형식
  keep_question = "relevance"     남길지 정하는 질문 이름
  keep_choice = "useful"          그 질문에서 이 선택지 확률이 threshold 이상이면 kept
  threshold = 0.3                 선택. 생략하면 JEV_KEEP_THRESHOLD
  [questions.<이름>]              Jev 에 보내는 questions 그대로 (type, instructions, criteria)

판별할 때마다 파일을 읽으므로 고친 뒤 워커를 재시작하지 않아도 된다.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass

from app.core.config import get_settings


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    questions: dict
    keep_question: str
    keep_choice: str
    threshold: float


def load(name: str) -> Prompt:
    settings = get_settings()
    path = settings.prompts_dir / f"{name}.toml"
    if not path.is_file():
        raise PromptError(f"prompt not found: {path}")
    with path.open("rb") as f:
        data = tomllib.load(f)

    questions = data.get("questions")
    keep_question = data.get("keep_question")
    keep_choice = data.get("keep_choice")
    if not isinstance(questions, dict) or not questions:
        raise PromptError(f"{path}: [questions] is required")
    if keep_question not in questions:
        raise PromptError(f"{path}: keep_question {keep_question!r} is not in [questions]")
    criteria = questions[keep_question].get("criteria") or {}
    if keep_choice not in criteria:
        raise PromptError(f"{path}: keep_choice {keep_choice!r} is not in questions.{keep_question}.criteria")
    threshold = float(data.get("threshold", settings.jev_keep_threshold))
    if not 0 <= threshold <= 1:
        raise PromptError(f"{path}: threshold must be between 0 and 1")
    return Prompt(name, questions, keep_question, keep_choice, threshold)
