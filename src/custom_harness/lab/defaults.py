"""Lab defaults (GOAL 18.6 P1, UPGRADES N19): optional skills, and dead ends that wait to be asked for.

The files go in `.lab/` in the workspace. Nothing is injected into a prompt and nothing limits the agent: it may read them
or ignore them. Past dead ends sit in a file, so an agent reads them when it wants them, because history pushed into
context can anchor an agent (UPGRADES conflict 6). They come from Lab ideas only: a Court verdict never reaches a box this
way.

Periodic context resets are advice in the checklist. The harness does not reset an agent's context: that is the agent's
own. What it does do is run each headless prompt as a fresh process.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..sandbox.base import safe_path

SKILLS = {
    "ideation.md": """# Ideation (optional)

Write three different ideas before testing any of them. For each, write what result would make you drop it.
Report ideas through `.lab/trace.jsonl`, so they reach the Story, including the ones you drop and why.
""",
    "experiments.md": """# Running experiments (optional)

Fix the baseline first, and make it an honest one. Change one thing at a time. Run several seeds before believing a
difference. Keep the exact command of every run. Compare against the numbers in any paper you rely on: if you cannot
reproduce its baseline, stop and find out why before building on it.
""",
    "checklist.md": """# Before asking to freeze (optional)

- The method beats the baseline on data the method was not tuned on.
- It runs from a clean start with its pinned packages.
- `method.py` and `baseline.py` read only what they are given, and write only predictions.
- The sentence of the claim says exactly what was measured, and no more.
- Long session? Save the best candidate and your notes to a file, then start the next stretch from that file alone. A
  fresh look often finds what a long context hides.
- Past dead ends are in `.lab/dead_ends.md`. Read them when you want them, not by habit.
""",
}


def dead_ends(ideas: list[dict[str, Any]]) -> str:
    lines = ["# Dead ends (read when you want them)", ""]
    lines += [f"- {i['title']}: {i['reason']}" for i in ideas] or ["None yet."]
    return "\n".join(lines) + "\n"


def write(workspace: Path, ideas: list[dict[str, Any]]) -> list[str]:
    """Put the pack in `.lab/`. `ideas`: abandoned Lab ideas, each with `title` and `reason`. Returns the paths."""
    done = []
    (workspace / ".lab" / "skills").mkdir(parents=True, exist_ok=True)
    for name, text in SKILLS.items():
        safe_path(workspace, f".lab/skills/{name}").write_text(text, encoding="utf-8")
        done.append(f".lab/skills/{name}")
    safe_path(workspace, ".lab/dead_ends.md").write_text(dead_ends(ideas), encoding="utf-8")
    safe_path(workspace, ".lab/pack.json").write_text(json.dumps({"format": "chh-lab-defaults/1", "files": done}),
                                                      encoding="utf-8")
    return [*done, ".lab/dead_ends.md"]
