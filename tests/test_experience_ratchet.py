"""Static ratchets of the Experience API (GOAL 18.5). One display path: a new reader of a Court certificate fails here
until it goes through court/display.py or joins the closed list with its reason."""

from __future__ import annotations

import ast
import typing
from pathlib import Path

from custom_harness.experience import schemas as M

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
API_FILES = sorted((SRC / "experience").glob("*.py"))

# file -> why it may call `.certificate(`. Everything else that shows a certificate goes through court/display.py.
CERTIFICATE_READERS = {
    "assure/corpus.py": "the honest corpus's own certificates",
    "assure/resweep.py": "re-checks past certificates, shows no Court case",
    "audit/checklist.py": "Claim Audit route: public data",
    "audit/claim_audit.py": "Claim Audit route: public data",
    "cli.py": "chh certificates, which points a Court case to `chh court status`",
    "court/display.py": "THE display path",
    "court/health.py": "scratch stores only",
    "court/levels.py": "computes badges, shown only by display.py",
    "court/rehearsal.py": "scratch stores only",
    "court/replicate.py": "reads the claim text only",
    "figures/report_figure.py": "research route",
    "kerneld/scope.py": "the bundle's dataset list",
    "lab/promote.py": "release_verdict: the human-released label and badges through display.emit_outcome",
    "memory/store.py": "research route",
    "memory/writer.py": "research route",
    "research/claims.py": "research route, with its own embargo",
    "research/memory_wiring.py": "research route",
    "research/perturbed.py": "research route",
    "research/station/archive.py": "research route",
    "research/station/mode.py": "research route",
    "scorecard/facts.py": "filtered by display.exposure",
    "scorecard/local_axes.py": "the evidence ids of certificates facts.py kept",
    "studio/explain.py": "gated by display.require_autopsy in studio/cli.py",
    "studio/graph.py": "gated by display.exposure",
    "studio/limitations.py": "reads the evidence class only",
}
FORBIDDEN_ATTRIBUTES = {"certificate", "ledger", "vault", "get_evidence", "ledger_rows", "failed", "approve_role",
                        "approve_adapter", "retire", "release_verdict", "open_alpha_account", "emit", "record_release",
                        "ledger_append", "latest", "events_of", "summary", "certify", "preregister"}
FORBIDDEN_IMPORTS = ("kernel", "kerneld.client", "kerneld.scope", "kerneld.releases", "court.levels", "court.replicate",
                     "human.actions", "human.nodes", "human.review", "human.features", "human.providers",
                     "human.checks", "human.queue", "human.cli")


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def test_every_reader_of_a_certificate_is_on_the_closed_list():
    found = set()
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith("kernel/") or "__pycache__" in rel:
            continue
        for c in _calls(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(c.func, ast.Attribute) and c.func.attr == "certificate":
                found.add(rel)
    assert found == set(CERTIFICATE_READERS)


def test_only_the_display_module_uses_the_outcome_of_a_case():
    """The outcome rows of `court_case` are read in court/ and by lab/promote.release_verdict, nowhere else."""
    allowed = {"court/display.py", "court/levels.py", "court/minted.py", "court/replicate.py", "court/health.py",
               "court/rehearsal.py", "lab/promote.py"}
    bad = []
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        text = p.read_text(encoding="utf-8")
        if rel not in allowed and ("court.minted import latest" in text or "minted import latest" in text):
            bad.append(rel)
    assert bad == []


def test_the_api_reaches_court_output_only_through_the_display_module():
    bad = []
    for p in API_FILES:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in FORBIDDEN_ATTRIBUTES:
                bad.append(f"{p.name}:{n.lineno} .{n.attr}")
            if isinstance(n, ast.ImportFrom):
                names = [n.module or "", *[f"{n.module}.{a.name}" if n.module else a.name for a in n.names]]
                if any(x == f or x.endswith(f".{f}") or x.startswith(f"{f}.") for x in names
                       for f in FORBIDDEN_IMPORTS):
                    bad.append(f"{p.name}:{n.lineno} imports {names[0]}")
    assert bad == []


def test_the_result_of_a_trial_is_dropped_never_read():
    tree = ast.parse((SRC / "experience" / "api.py").read_text(encoding="utf-8"))
    runs = [c for c in _calls(tree) if isinstance(c.func, ast.Attribute) and c.func.attr == "run"
            and isinstance(c.func.value, ast.Name) and c.func.value.id == "C"]
    assert runs  # not vacuous: the verb exists
    dropped = [n.value for n in ast.walk(tree) if isinstance(n, ast.Expr)]
    assert all(c in dropped for c in runs)


def test_the_cli_surfaces_of_a_certificate_keep_their_gates():
    assert "require_autopsy" in (SRC / "studio" / "cli.py").read_text(encoding="utf-8")
    root = (SRC / "cli.py").read_text(encoding="utf-8")
    assert "require_autopsy" in root and "released_scores" in root
    assert "exposure" in (SRC / "studio" / "graph.py").read_text(encoding="utf-8")
    assert "exposure" in (SRC / "scorecard" / "facts.py").read_text(encoding="utf-8")


def _numeric(tp: object) -> bool:
    if tp in (int, float):
        return True
    return any(_numeric(a) for a in typing.get_args(tp) if a is not type(None))


def test_every_number_on_a_main_view_is_a_link():
    seen, bad = set(), []

    def walk(model: type[M.Model]) -> None:
        if model in seen or model is M.Number:
            return
        seen.add(model)
        for name, f in model.model_fields.items():
            if _numeric(f.annotation):
                bad.append(f"{model.__name__}.{name}")
            stack = [f.annotation]
            while stack:
                t = stack.pop()
                if isinstance(t, type) and issubclass(t, M.Model):
                    walk(t)
                stack += list(typing.get_args(t))

    for view in M.MAIN_VIEWS:
        walk(view)
    assert bad == [] and M.Case in seen and len(seen) > 20
