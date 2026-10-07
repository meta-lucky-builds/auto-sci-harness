"""Question-only critic filter (AHOIS, GOAL L4.8), enforced in code.

A kept item is ONE genuine question: it ends with '?', has exactly one '?', is 10-300 characters long, contains no
code, and is not a directive dressed as a question. Directive shapes recognised (case-insensitive):

  - imperative openings: "use ...", "try ...", "replace ...", "you should ...", "instead ...";
  - requests and proposals: "could/can/would/will you|we ...", "why not ...", "why don't you|we ...",
    "how about ...", "what about <solution-verb>ing ...", "have you tried ...", "have you considered <verb>ing ...",
    "should/shouldn't you|we ...", "wouldn't it be better ...", "is it not better ...";
  - embedded advice anywhere: "you should/could/might/need to/ought to", "better to/if/off", "instead of",
    "the answer is", "the fix is", "the solution is", "I would/I'd/I suggest/I recommend";
  - assignments (k=25, C = 0.1) and code tokens.

This is a HEURISTIC (stated, not hidden): it reads surface form only. A leading question that embeds a solution
without any of these shapes ("Is a gradient-boosted model with depth 3 the natural choice here?") passes, and a few
genuine questions using these shapes are dropped. tests/test_evolve_hardening.py measures it on a small labelled set
and pins the confusion counts.
"""

from __future__ import annotations

import re

_CODEISH = ("```", "def ", "import ", "lambda", "==", "()", "->", "sklearn.", "np.", "pd.")
_DIRECTIVE_STARTS = ("use ", "try ", "consider ", "you should", "instead", "add ", "remove ", "replace ", "switch ",
                     "apply ", "set ", "drop ", "increase ", "decrease ", "tune ", "scale ", "normalize ",
                     "normalise ", "please ", "make sure", "ensure ", "go with", "let's ", "lets ")
_SOLUTION_VERB = (r"(?:use|using|add|adding|switch|switching|replac\w*|swap\w*|try|trying|apply\w*|tun\w*|set\w*|"
                  r"increas\w*|decreas\w*|drop\w*|remov\w*|scal\w*|normali[sz]\w*|standardi[sz]\w*|"
                  r"encod\w*|ensembl\w*|stack\w*|regulari[sz]\w*|chang\w*|go\w*|implement\w*|fit\w*|train\w*)")
_DIRECTIVE_PATTERNS = [re.compile(p) for p in (
    r"^(?:could|can|would|will|won't|wouldn't|couldn't)\s+(?:you|we)\b",
    r"^why\s+not\b",
    r"^why\s+(?:don't|do\s+not|didn't|did\s+not|wouldn't|won't)\s+(?:you|we)\b",
    r"^how\s+about\b",
    r"^what\s+about\s+" + _SOLUTION_VERB + r"\b",
    r"^have\s+you\s+(?:tried|thought\s+(?:about|of)\s+" + _SOLUTION_VERB + r")\b",
    r"^have\s+you\s+considered\s+" + _SOLUTION_VERB + r"\b",
    r"^(?:should|shouldn't|must|mustn't)\s+(?:you|we)\b",
    r"^(?:wouldn't|isn't|is)\s+it\s+(?:not\s+)?(?:better|wiser|simpler|easier)\b",
    r"\byou\s+(?:should|could|might|may|need\s+to|ought\s+to|must|have\s+to)\b",
    r"\bbetter\s+(?:to|if|off)\b",
    r"\binstead\s+of\b",
    r"\bthe\s+(?:answer|fix|solution|right\s+approach)\s+is\b",
    r"\bi\s+(?:would|'d|suggest|recommend|propose)\b|\bi'd\b",
    r"\b\w+\s*=\s*[\w.\-]+",
)]


def is_genuine_question(text: str) -> bool:
    text = str(text).strip()
    low = " ".join(text.lower().split())
    if not (text.endswith("?") and text.count("?") == 1 and 10 <= len(text) <= 300):
        return False
    if any(c in low for c in _CODEISH) or low.startswith(_DIRECTIVE_STARTS):
        return False
    return not any(p.search(low) for p in _DIRECTIVE_PATTERNS)
