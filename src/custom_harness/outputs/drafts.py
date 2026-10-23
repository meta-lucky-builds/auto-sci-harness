"""Lab drafts are EXPLORATORY (GOAL §17: "Lab outputs are labelled EXPLORATORY and are never findings").

Every draft, report or paper the Lab writes goes through `label_markdown` / `label_latex`, which put a visible
banner at the top of the document (and, for LaTeX, a machine-readable comment as the first line). The Court's paper
generator (court_paper.py) refuses any input that carries the label: a Lab draft never becomes a Court paper by
being copied; it has to be promoted (pre-registered, frozen and re-executed by the Court). Built, NOT test-verified.
"""

from __future__ import annotations

import re

LABEL = "EXPLORATORY"
BANNER = ("EXPLORATORY: Lab output, not a finding. No number in this document is certified by the Evidence Kernel; "
          "only a Court certificate can make a claim.")
TEX_MARK = "% chh-label: EXPLORATORY"
_MD_MARK = f"> **{BANNER}**"
_BEGIN_DOC = re.compile(r"\\begin\s*\{document\}")


class ExploratoryInCourt(PermissionError):
    """A Lab (EXPLORATORY) document was handed to the Court path."""


def label_markdown(text: str) -> str:
    if text.startswith(_MD_MARK):
        return text
    return f"{_MD_MARK}\n\n{text}"


def label_latex(tex: str) -> str:
    """Prefix the machine-readable mark and put a framed banner right after \\begin{document} (or at the top of a
    body without one)."""
    if tex.startswith(TEX_MARK):
        return tex
    banner = ("\\noindent\\fbox{\\parbox{\\dimexpr\\linewidth-2\\fboxsep-2\\fboxrule}{\\textbf{" + BANNER +
              "}}}\\par\\medskip\n")
    m = _BEGIN_DOC.search(tex)
    body = (tex[:m.end()] + "\n" + banner + tex[m.end():]) if m else banner + tex
    return f"{TEX_MARK}\n{body}"


def is_exploratory(text: str) -> bool:
    return text.startswith(TEX_MARK) or text.startswith(_MD_MARK) or BANNER in text or bool(
        re.search(r"(?i)\bexploratory\b\s*:\s*lab output", text))


def refuse_in_court(text: str) -> None:
    if is_exploratory(text):
        raise ExploratoryInCourt("this document is a Lab draft (EXPLORATORY); the Court generates papers only from "
                                 "certificates (promote the claim instead)")
