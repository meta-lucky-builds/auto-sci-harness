"""Court papers and reports generated FROM CERTIFICATES (GOAL §17 "The Court"; invariant 8: every number in a report
resolves to an artifact and is recomputed by the audit).

The author (human or agent) supplies a LaTeX BODY that may contain no number of its own. Every number is a
placeholder `\\chhnum{ref}` (or `\\chhnum[fmt]{ref}`), and `refs` maps each ref to a Kernel ledger certificate and a
JSON pointer into it ({"certificate": <hash>, "pointer": "/resolution/..."}). Raw evidence refs are refused here:
the Court reports certified results only. The generator:
1. refuses an EXPLORATORY (Lab) input (drafts.refuse_in_court);
2. refuses commands that could print a number or pull in other text (\\input, \\include, \\def, \\newcommand,
   \\the, \\number, \\setcounter, \\csname, \\catcode, \\char, \\num, \\SI, ...);
3. refuses ANY digit left in the body after removing comments, the placeholders and the arguments of reference
   commands (\\label, \\ref, \\eqref, \\cite*, \\includegraphics, \\begin/\\end);
4. resolves every ref with figures.audit.resolve_refs against the certificates the caller allows (fail closed);
5. renders each value with a fixed formatter and wraps the body in a FIXED preamble (the author cannot define
   macros there).
It returns the .tex and a manifest {refs + resolved value + rendered text, template and tex hashes}.
`audit_court_paper` re-resolves the refs, re-renders, and requires byte-identical output: a paper whose numbers no
longer match its certificates fails.

Known limits (stated): numbers spelled in words ("ninety-three") are not detected; LaTeX's own counters (section and
figure numbers) and the bibliography's contents are not data and are not checked; the PDF compile happens in the
sandbox (latex.py) and the PDF itself is not re-parsed. Built, NOT test-verified.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..discovery_trace import TraceSink, safe_emit
from ..figures.audit import FigureError, resolve_refs
from .drafts import refuse_in_court

FORMAT = "chh-court-paper/1"
FORMATS = {"g": ".6g", "2f": ".2f", "3f": ".3f", "4f": ".4f", "e": ".3e"}
PREAMBLE = ("% " + FORMAT + ": generated from Kernel certificates; every number resolves to a certificate "
            "(invariant 8)\n"
            "\\documentclass{article}\n\\usepackage[T1]{fontenc}\n\\usepackage{lmodern}\n\\usepackage{amsmath}\n"
            "\\usepackage{graphicx}\n\\usepackage[hidelinks]{hyperref}\n")
_PLACEHOLDER = re.compile(r"\\chhnum(?:\[(?P<fmt>[a-z0-9]{1,3})\])?\{(?P<ref>[A-Za-z_][A-Za-z0-9_]{0,63})\}")
_COMMENT = re.compile(r"(?<!\\)%.*")
_REF_ARGS = re.compile(r"\\(?:label|ref|eqref|autoref|cref|Cref|pageref|cite[a-zA-Z]*|includegraphics|begin|end)"
                       r"\*?(?:\[[^\]]*\])?\{[^}]*\}")
_FORBIDDEN = re.compile(r"\\(?:input|include|includeonly|def|edef|gdef|xdef|let|newcommand|renewcommand|"
                        r"providecommand|DeclareRobustCommand|newenvironment|renewenvironment|the|number|"
                        r"romannumeral|setcounter|addtocounter|stepcounter|refstepcounter|value|arabic|roman|"
                        r"csname|catcode|char|mathchar|chardef|uppercase|lowercase|string|detokenize|scantokens|"
                        r"num|SI|qty|si|pdfstrcmp|directlua|write|immediate|openin|read|usepackage|"
                        r"documentclass)(?![A-Za-z])")


class CourtPaperError(ValueError):
    pass


Certificates = Callable[[str], Any]


@dataclass(frozen=True)
class CourtPaper:
    tex: str
    manifest: dict[str, Any]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_problems(body: str) -> list[str]:
    """Every reason `body` may not be a Court paper body (empty list = acceptable)."""
    problems: list[str] = []
    no_comments = _COMMENT.sub("", body)
    for m in _FORBIDDEN.finditer(no_comments):
        problems.append(f"command {m.group(0)} is not allowed in a Court paper body")
    stripped = _REF_ARGS.sub("", _PLACEHOLDER.sub("", no_comments))
    for m in re.finditer(r"\d[\d.,]*", stripped):
        lo = max(0, m.start() - 30)
        problems.append(f"free number in the text near {stripped[lo:m.start() + 10]!r}: numbers must come from "
                        "\\chhnum{ref}")
        if len(problems) > 20:
            break
    for m in _PLACEHOLDER.finditer(no_comments):
        if m.group("fmt") and m.group("fmt") not in FORMATS:
            problems.append(f"unknown number format [{m.group('fmt')}] (allowed: {sorted(FORMATS)})")
    if "\\chhnum" in _PLACEHOLDER.sub("", no_comments):
        problems.append("malformed \\chhnum placeholder")
    return problems


def render_value(v: float, fmt: str | None) -> str:
    if not math.isfinite(v):
        raise CourtPaperError("a certified value is not finite")
    s = format(v, FORMATS[fmt or "g"])
    return s.replace("-", "$-$", 1) if s.startswith("-") else s


def generate(title: str, body: str, refs: Mapping[str, Mapping[str, str]], *, certificate_of: Certificates,
             allowed_certificates: set[str], trace: TraceSink | None = None) -> CourtPaper:
    refuse_in_court(title)
    refuse_in_court(body)
    problems = body_problems(title) + body_problems(body)
    if problems:
        raise CourtPaperError("; ".join(problems))
    used = {m.group("ref") for m in _PLACEHOLDER.finditer(_COMMENT.sub("", body))}
    if not isinstance(refs, Mapping) or set(refs) != used:
        raise CourtPaperError(f"refs must name exactly the placeholders used: missing {sorted(used - set(refs))}, "
                              f"unused {sorted(set(refs) - used)}")
    for rid, ref in refs.items():
        if not isinstance(ref, Mapping) or set(ref) != {"certificate", "pointer"}:
            raise CourtPaperError(f"ref {rid}: a Court paper cites certificates only ({{certificate, pointer}})")
    try:
        values = resolve_refs({k: dict(v) for k, v in refs.items()}, None, set(), certificate_of=certificate_of,
                              allowed_certificates=allowed_certificates) if refs else {}
    except FigureError as e:
        raise CourtPaperError(str(e)) from e
    rendered: dict[str, str] = {}

    def sub(m: re.Match[str]) -> str:
        text = render_value(values[m.group("ref")], m.group("fmt"))
        rendered[f"{m.group('ref')}[{m.group('fmt') or 'g'}]"] = text
        return text

    out_body = _PLACEHOLDER.sub(sub, _COMMENT.sub("", body))  # author comments are not carried over
    tex = (PREAMBLE + "\\title{" + title + "}\n\\date{}\n\\begin{document}\n\\maketitle\n" + out_body +
           "\n\\end{document}\n")
    manifest = {"format": FORMAT, "title_sha256": _sha(title), "body_sha256": _sha(body),
                "refs": {rid: {**dict(refs[rid]), "value": values[rid]} for rid in sorted(refs)},
                "rendered": dict(sorted(rendered.items())), "tex_sha256": _sha(tex)}
    safe_emit(trace, "output.paper_built", _sha(tex)[:16], {
        "tex_sha256": _sha(tex), "certificates": sorted({str(r["certificate"]) for r in refs.values()}),
        "manifest_sha256": _sha(json.dumps(manifest, sort_keys=True))})
    return CourtPaper(tex, manifest)


def audit_court_paper(title: str, body: str, tex: str, manifest: Mapping[str, Any], *,
                      certificate_of: Certificates, allowed_certificates: set[str]) -> list[str]:
    """Re-generate from the certificates and compare. Empty list = the paper's numbers still match."""
    problems: list[str] = []
    try:
        again = generate(title, body, {k: {"certificate": v["certificate"], "pointer": v["pointer"]}
                                       for k, v in (manifest.get("refs") or {}).items()},
                         certificate_of=certificate_of, allowed_certificates=allowed_certificates)
    except (CourtPaperError, KeyError, TypeError, PermissionError) as e:
        return [f"cannot regenerate the paper: {e}"]
    if again.tex != tex:
        problems.append("the paper differs from a fresh render from its certificates")
    if json.dumps(again.manifest, sort_keys=True) != json.dumps(dict(manifest), sort_keys=True):
        problems.append("the manifest differs from a fresh render from its certificates")
    return problems
