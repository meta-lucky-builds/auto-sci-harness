"""Research outputs (outputs/): Court papers only from certificates, EXPLORATORY Lab drafts, sandbox-only LaTeX with
hardened settings, vendored figure assets with their notice. WRITTEN, NOT RUN (lab-compute, build-only phase).

Fail-first reasoning: outputs/ does not exist on origin/int17. The Court tests fail if the free-number check, the
forbidden-command check, the certificate-only rule or the re-render audit is removed.
"""

from __future__ import annotations

import pytest

from custom_harness.discovery_trace import MemorySink
from custom_harness.outputs import court_paper as C
from custom_harness.outputs import drafts as D
from custom_harness.outputs import latex as L

CERT = {"resolution": {"targets": {"acc": {"lo": 0.912345, "hi": 0.95}}}}
REFS = {"lo": {"certificate": "h1", "pointer": "/resolution/targets/acc/lo"}}
BODY = r"The certified lower bound is \chhnum[3f]{lo} (Table~\ref{tab:1})."


def _gen(body=BODY, refs=REFS, cert=CERT, allowed=frozenset({"h1"}), **kw):
    return C.generate("A certified result", body, refs, certificate_of=lambda h: cert,
                      allowed_certificates=set(allowed), **kw)


def test_court_paper_numbers_come_only_from_certificates():
    sink = MemorySink()
    p = _gen(trace=sink)
    assert "0.912" in p.tex and p.manifest["refs"]["lo"]["value"] == 0.912345
    assert sink.events[0].kind == "output.paper_built" and sink.events[0].fields["certificates"] == ["h1"]


@pytest.mark.parametrize("body", [r"Accuracy was 0.93.", r"We gained 12% \chhnum{lo}", r"In 2024 we saw \chhnum{lo}",
                                  r"\newcommand{\x}{\chhnum{lo}} \x", r"\input{numbers} \chhnum{lo}",
                                  r"\the\year \chhnum{lo}", r"\num{3} \chhnum{lo}"])
def test_court_paper_refuses_free_numbers_and_number_making_commands(body):
    with pytest.raises(C.CourtPaperError):
        _gen(body=body)


def test_court_paper_refuses_raw_evidence_refs_and_unallowed_certificates():
    with pytest.raises(C.CourtPaperError):
        _gen(refs={"lo": {"evidence": "e1", "pointer": "/score"}})
    with pytest.raises(C.CourtPaperError):
        _gen(allowed=frozenset())


def test_court_paper_refuses_a_lab_draft():
    with pytest.raises(D.ExploratoryInCourt):
        _gen(body=D.label_markdown(BODY))


def test_the_audit_fails_when_the_certificate_no_longer_gives_the_printed_number():
    p = _gen()
    ok = C.audit_court_paper("A certified result", BODY, p.tex, p.manifest, certificate_of=lambda h: CERT,
                             allowed_certificates={"h1"})
    assert ok == []
    other = {"resolution": {"targets": {"acc": {"lo": 0.5}}}}
    bad = C.audit_court_paper("A certified result", BODY, p.tex, p.manifest, certificate_of=lambda h: other,
                              allowed_certificates={"h1"})
    assert bad
    edited = p.tex.replace("0.912", "0.991")
    assert C.audit_court_paper("A certified result", BODY, edited, p.manifest, certificate_of=lambda h: CERT,
                               allowed_certificates={"h1"})


def test_lab_drafts_are_labelled_exploratory():
    md = D.label_markdown("# Idea\nresult 0.93")
    assert md.startswith("> **EXPLORATORY") and D.is_exploratory(md) and D.label_markdown(md) == md
    tex = D.label_latex("\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n")
    assert tex.startswith(D.TEX_MARK) and tex.index("EXPLORATORY: Lab output") > tex.index("\\begin{document}")


def test_latex_arguments_are_hardened():
    a = L.pass_args(L.Driver.LATEXMK, L.Program.PDF, "out", "paper.tex")
    assert "-norc" in a and "-no-shell-escape" in a and a[-1] == "./paper.tex"
    t = L.pass_args(L.Driver.TECTONIC, L.Program.XE, "out", "paper.tex")
    assert "--only-cached" in t
    assert L.SAFE_TEX_ENV["openin_any"] == "p" and L.SAFE_TEX_ENV["openout_any"] == "p"
    with pytest.raises(ValueError):
        L.pass_args(L.Driver.DIRECT, L.Program.PDF, "out", "-shell-escape.tex")


def test_latex_program_and_driver_choice():
    assert L.program_from_source("% !TeX program = xelatex\n\\documentclass{x}") is L.Program.XE
    assert L.choose_driver(L.Program.PDF, lambda t: t == "tectonic") is L.Driver.TECTONIC
    assert L.choose_driver(L.Program.PDF, lambda t: False) is None
    assert L.reports_errors("ok\n! Undefined control sequence.\n")


def test_latex_never_compiles_on_the_host(tmp_path):
    from custom_harness.sandbox import LocalSandbox
    (tmp_path / "p.tex").write_text("x")
    with pytest.raises(L.CompileRefused):
        L.compile_in_sandbox(LocalSandbox(tmp_path, i_understand_no_isolation=True), "p.tex",
                             available=lambda t: True)


def test_figure_assets_are_vendored_with_their_notice(tmp_path):
    from custom_harness.outputs.figstyle import ASSET_DIR, vendor_into
    notice = (ASSET_DIR / "NOTICE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 alphaXiv" in notice and "MIT License" in notice
    for f in ("orx_figstyle.py", "orx_chart.py", "orx-chart.html", "orx-tikz-preamble.tex"):
        assert "Copyright (c) 2026 alphaXiv" in (ASSET_DIR / f).read_text(encoding="utf-8").splitlines()[0]
    written = vendor_into(tmp_path)
    assert {p.name for p in written} >= {"orx_figstyle.py", "NOTICE"}
