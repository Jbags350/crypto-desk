"""Loads the editable agent prompts from the prompts/ folder.

Prompts are re-read on every call, so you can edit a prompt file while the
bot is running and the next cycle uses the new version. Notes inside
<!-- ... --> are for you and are never sent to the AI.
"""

from __future__ import annotations

import re
from pathlib import Path

from desk.config import PROJECT_ROOT

PROMPTS_DIR = PROJECT_ROOT / "prompts"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


class PromptError(Exception):
    pass


def load_prompt(name: str, directory: Path | None = None) -> str:
    path = (directory or PROMPTS_DIR) / f"{name}.md"
    if not path.exists():
        raise PromptError(f"Prompt file missing: {path}")
    text = _COMMENT.sub("", path.read_text(encoding="utf-8")).strip()
    if len(text) < 50:
        raise PromptError(f"Prompt file looks empty: {path}")
    return text
