from __future__ import annotations

import sqlite3

from app.services import jev, prompts
from app.services.render import render


def classify_event(event: sqlite3.Row) -> tuple[str, dict]:
    """(status, verdict) 를 돌려준다. status 는 kept | discarded."""
    state = render(event)
    if state is None:
        # 판별할 텍스트가 없으면 놓치지 않도록 일단 남긴다.
        return "kept", {"reason": "no_text"}
    prompt = prompts.load(event["source"])
    result = jev.classify(state, prompt.questions)
    useful = result["answers"][prompt.keep_question]["probabilities"][prompt.keep_choice]
    status = "kept" if useful >= prompt.threshold else "discarded"
    return status, {"useful": useful, "prompt": prompt.name, "model": result.get("model"),
                    "answers": result["answers"], "usage": result.get("usage")}
