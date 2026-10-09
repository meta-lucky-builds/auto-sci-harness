"""GOAL §5 L2 + invariant 8 extended to figures: the research report renders a figure (the held-out effect with its
interval and the required minimum effect, from the run's Kernel certificate) by a script stored next to it; every
plotted value resolves to a ledger artifact; the I6 audit re-renders it, and an injected fake value is blocked."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.figures import FigureError, audit_figures
from custom_harness.figures import svg as S
from custom_harness.figures.audit import audit_figure, check_svg, make_figure
from custom_harness.figures.report_figure import SCRIPT_TEMPLATE, figure_spec, make_report_figure
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_research import rings_task, scripted_llm  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[1]
H = "a" * 64
CERT = {"resolution": {"targets": {"accuracy": {"estimate": 0.12, "lo": 0.07, "hi": 0.17, "alpha_eff": 0.05}}},
        "claim": {"effect": {"min_effect": 0.02}}}


class _Kernel:
    def __init__(self, certs: dict[str, dict]):
        self.certs = certs

    def certificate(self, h: str):
        doc = self.certs[h]  # KeyError for an unknown certificate
        return type("C", (), {"model_dump": lambda self, mode="json": json.loads(json.dumps(doc))})()


@pytest.fixture
def sb(tmp_path):
    return LocalSandbox(tmp_path / "box", i_understand_no_isolation=True)


def _audit(fig_dir: Path, sb, certs: dict[str, dict], meta_sha: str | None):
    k = _Kernel(certs)
    return audit_figure(fig_dir, None, sb, allowed_evidence=set(), meta_sha256=meta_sha,
                        certificate_of=lambda h: k.certificate(h).model_dump(), allowed_certificates={H})


def test_report_figure_plots_only_certificate_refs_and_passes_the_audit(tmp_path, sb):
    fig = make_report_figure({"certificate": {"hash": H}}, _Kernel({H: CERT}), None, sb, tmp_path / "fig")
    assert fig is not None
    meta = json.loads((tmp_path / "fig/figure.json").read_text())
    assert {r["certificate"] for r in meta["refs"].values()} == {H}  # every plotted value resolves to the ledger
    svg = (tmp_path / "fig/figure.svg").read_text()
    script = (tmp_path / "fig/figure.py").read_text()  # stored next to the figure
    assert not re.search(r"\d\.\d", script)  # the script names refs, never numbers
    for v in (0.12, 0.07, 0.17, 0.02):
        assert f'data-v="{S.data_v(v)}"' in svg
    res = _audit(tmp_path / "fig", sb, {H: CERT}, fig["meta_sha256"])
    assert res.ok, res.problems


def test_an_injected_fake_value_is_blocked(tmp_path, sb):
    fig = make_report_figure({"certificate": {"hash": H}}, _Kernel({H: CERT}), None, sb, tmp_path / "fig")
    assert fig is not None
    svg_path = tmp_path / "fig/figure.svg"
    good = svg_path.read_text()
    # the attack: the effect's lower bound is drawn higher than the certificate says, and the stored hashes are
    # rewritten to match, so only the re-resolution and re-render can catch it
    fake = good.replace(f'data-v="{S.data_v(0.07)}"', f'data-v="{S.data_v(0.10)}"')
    assert fake != good
    svg_path.write_text(fake)
    meta_path = tmp_path / "fig/figure.json"
    meta = json.loads(meta_path.read_text())
    import hashlib
    meta["svg_sha256"] = hashlib.sha256(fake.encode()).hexdigest()
    meta_path.write_text(json.dumps(meta, sort_keys=True, indent=1))
    res = _audit(tmp_path / "fig", sb, {H: CERT}, None)
    assert not res.ok
    assert any(p.startswith("F3") for p in res.problems) and any(p.startswith("F4") for p in res.problems)
    # recorded meta hash: also caught at F1
    assert not _audit(tmp_path / "fig", sb, {H: CERT}, fig["meta_sha256"]).ok


def test_a_ledger_value_that_changed_is_blocked(tmp_path, sb):
    fig = make_report_figure({"certificate": {"hash": H}}, _Kernel({H: CERT}), None, sb, tmp_path / "fig")
    assert fig is not None
    moved = json.loads(json.dumps(CERT))
    moved["resolution"]["targets"]["accuracy"]["estimate"] = 0.15  # the figure no longer matches its artifact
    res = _audit(tmp_path / "fig", sb, {H: moved}, fig["meta_sha256"])
    assert not res.ok and any("F3" in p for p in res.problems)


def test_a_script_with_a_raw_number_or_a_foreign_certificate_is_refused(tmp_path, sb):
    items, refs = figure_spec([("claim", H, CERT)])
    script = SCRIPT_TEMPLATE.format(items=items, title="t", y_label="y").replace(
        "inp = S.load_inputs()", "inp = S.load_inputs()\nITEMS[0][1] = 0.5")  # a number instead of a ref
    k = _Kernel({H: CERT})
    with pytest.raises(FigureError):
        make_figure(tmp_path / "f1", script, refs, None, sb, set(),
                    certificate_of=lambda h: k.certificate(h).model_dump(),
                    allowed_certificates={H})
    other = "b" * 64
    k2 = _Kernel({H: CERT, other: CERT})
    refs2 = {**refs, "c0_t0_est": {"certificate": other, "pointer": "/resolution/targets/accuracy/estimate"}}
    good = SCRIPT_TEMPLATE.format(items=items, title="t", y_label="y")
    with pytest.raises(FigureError, match="not among the certificates"):
        make_figure(tmp_path / "f2", good, refs2, None, sb, set(),
                    certificate_of=lambda h: k2.certificate(h).model_dump(), allowed_certificates={H})
    with pytest.raises(FigureError):  # no resolver at all: a certificate ref fails closed
        make_figure(tmp_path / "f3", good, refs, None, sb, set())


def test_interval_marks_must_be_bound_and_drawn_where_the_values_put_them():
    inp = {"e": 0.1, "l": 0.05, "h": 0.2}
    svg = S.interval_chart(inp, [("x", "e", "l", "h")], title="t", y_label="y")
    assert check_svg(svg.encode(), inp) == []
    unbound = svg.replace('data-ref="l"', 'data-ref="zz"')
    assert check_svg(unbound.encode(), inp)
    moved = re.sub(r'(<line class="ci" data-ref="h" data-v="[^"]+" x1="[^"]+" y1="[^"]+" x2="[^"]+" y2=")[^"]+"',
                   r'\g<1>41.00"', svg)
    assert moved != svg and check_svg(moved.encode(), inp)


def test_v1_report_renders_an_audited_figure_and_a_tampered_one_fails_i6(store, tmp_path, rings_task,  # noqa: F811
                                                                       monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    raw = yaml.safe_load((ROOT / "examples/research_v1.yaml").read_text(encoding="utf-8"))
    assert raw["nodes"]["report"]["params"]["figure"] is True
    llm = MockBackend(scripted_llm)
    eng = Engine(HarnessSpec.model_validate(raw), backends={"proposer": llm, "executor": llm, "judge": llm,
                                                            "replicator": llm},
                 store=store, sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL"
    (fig,) = s["figures"]
    assert "## Figures" in s["report"] and fig["meta_sha256"] in s["report"]
    assert s["audit"]["I6"] is True and s["audit"]["passed"], s["audit"]
    kernel = Kernel(store)
    certs = {s["certificate"]["hash"], *([s["replica_certificate"]["hash"]] if s.get("replica_certificate") else [])}
    svg_path = Path(fig["dir"]) / "figure.svg"
    good = svg_path.read_text(encoding="utf-8")
    m = re.search(r'<circle class="mark" data-ref="([^"]+)" data-v="([^"]+)"', good)
    assert m
    fake = good.replace(f'data-v="{m.group(2)}"', f'data-v="{S.data_v(float(m.group(2)) + 0.05)}"', 1)
    svg_path.write_text(fake, encoding="utf-8")  # the attack: one plotted value inflated after the run

    def again():
        return audit_figures(s["figures"], store, lambda i: LocalSandbox(tmp_path / f"re{i}",
                                                                         i_understand_no_isolation=True),
                             {str(e) for e in [*(s.get("evidence_ids") or []), *(s.get("replica_evidence_ids") or [])]},
                             certificate_of=lambda h: kernel.certificate(h).model_dump(mode="json"),
                             allowed_certificates=certs)

    bad = again()
    assert bad["I6"] is False and bad["problems"], bad
    svg_path.write_text(good, encoding="utf-8")
    assert again()["I6"] is True
