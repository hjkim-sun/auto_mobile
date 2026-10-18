from __future__ import annotations

import sqlite3

from app.core.config import get_settings
from app.services import jev
from app.services.render import render


def classify_event(event: sqlite3.Row) -> tuple[str, dict]:
    """(status, verdict) 를 돌려준다. status 는 kept | discarded."""
    state = render(event)
    if state is None:
        # 판별할 텍스트가 없으면 놓치지 않도록 일단 남긴다.
        return "kept", {"reason": "no_text"}
    result = jev.classify(state)
    useful = result["answers"]["relevance"]["probabilities"]["useful"]
    status = "kept" if useful >= get_settings().jev_keep_threshold else "discarded"
    return status, {"useful": useful, "model": result.get("model"),
                    "answers": result["answers"], "usage": result.get("usage")}
