"""GOAL §5 L2 "Figures" (invariant 8 extended): a figure is rendered by a script stored next to it, every plotted
value resolves to an Evidence artifact, and the audit re-renders the figure and checks the plotted data and bytes.
Attack tests assert that each attack is in place before asserting that it is caught."""

import json
from pathlib import Path

import pytest

from custom_harness.records import Evidence, EvidenceKind
from custom_harness.sandbox import LocalSandbox
from custom_harness.tiers import Tier

HONEST = """import chh_svg as S
inp = S.load_inputs()
S.save(S.bar_chart(inp, [("ours", "ours"), ("baseline", "base")], title="Held-out accuracy", y_label="accuracy"))
"""
LINE = """import chh_svg as S
inp = S.load_inputs()
S.save(S.line_chart(inp, [("ours", ["ours", "ours2"]), ("baseline", ["base", "base2"])], x_labels=["seed a", "seed b"],
                    title="Per seed"))
"""


def _ev(store, doc) -> str:
    ev = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T2_STATISTICAL, verifier_id="test", produced_by_run=None,
                  payload_ref=store.blobs.put_json(doc))
    store.add_evidence(ev)
    return ev.id


@pytest.fixture
def world(store, tmp_path):
    ids = {"ours": _ev(store, {"score": 0.91, "arm": "subject"}), "base": _ev(store, {"score": 0.62}),
           "ours2": _ev(store, {"score": 0.88}), "base2": _ev(store, {"score": 0.6})}
    refs = {k: {"evidence": v, "pointer": "/score"} for k, v in ids.items()}
    n = {"i": 0}

    def box():
        n["i"] += 1
        return LocalSandbox(tmp_path / f"fig-sb-{n['i']}", i_understand_no_isolation=True)
    return store, refs, box, tmp_path


def _make(world, script=HONEST, name="fig"):
    from custom_harness.figures import make_figure
    store, refs, box, tmp = world
    d = tmp / name
    return d, make_figure(d, script, refs, store, box())


def _audit(world, d, **kw):
    from custom_harness.figures import audit_figure
    store, _, box, _ = world
    return audit_figure(d, store, box(), **kw)


def _reseal(d: Path) -> None:
    """The attacker also updates figure.json's hashes, so only the deeper checks can catch the edit."""
    import hashlib
    meta = json.loads((d / "figure.json").read_bytes())
    meta["svg_sha256"] = hashlib.sha256((d / "figure.svg").read_bytes()).hexdigest()
    meta["script_sha256"] = hashlib.sha256((d / "figure.py").read_bytes()).hexdigest()
    (d / "figure.json").write_bytes(json.dumps(meta, sort_keys=True, indent=1).encode())


# ------------------------------------------------------------------ honest figures
def test_honest_bar_figure_passes_and_rerenders_byte_identically(world):
    d, meta_sha = _make(world)
    svg = (d / "figure.svg").read_text(encoding="utf-8")
    assert 'data-v="0.91"' in svg and 'data-v="0.62"' in svg and (d / "figure.py").read_text() == HONEST
    res = _audit(world, d, meta_sha256=meta_sha)
    assert res.ok, res.problems
    assert res.values == {"ours": 0.91, "base": 0.62, "ours2": 0.88, "base2": 0.6}


def test_honest_line_figure_passes(world):
    d, meta_sha = _make(world, LINE)
    assert _audit(world, d, meta_sha256=meta_sha).ok


# ------------------------------------------------------------------ attacks: fabricated numbers
def test_attack_script_passing_a_raw_number_is_refused_by_the_renderer(world):
    from custom_harness.figures import FigureError
    with pytest.raises(FigureError, match="ref ids"):
        _make(world, HONEST.replace('("ours", "ours")', '("ours", 0.99)'))


def test_attack_fabricated_value_in_the_stored_svg(world):
    d, _ = _make(world)
    p = d / "figure.svg"
    p.write_text(p.read_text(encoding="utf-8").replace('data-v="0.91"', 'data-v="0.99"').replace(">0.91<", ">0.99<"),
                 encoding="utf-8", newline="\n")
    assert 'data-v="0.99"' in p.read_text(encoding="utf-8")  # the attack is in place
    _reseal(d)
    res = _audit(world, d)
    assert not res.ok and any("F3 mark ours: data-v" in x for x in res.problems), res.problems
    assert any(x.startswith("F4") for x in res.problems)


def test_attack_script_that_draws_a_fabricated_bar_by_hand(world):
    """The script writes its own SVG: the value is the true one, but the bar is drawn for 0.99."""
    from custom_harness.figures import svg as S
    true = S.bar_chart({"ours": 0.91, "base": 0.62}, [("ours", "ours"), ("baseline", "base")], title="t")
    lo, hi, _ = S.axis([0.91, 0.62])
    g_true, g_fake = S.bar_geometry(2, 0, lo, hi, 0.91), S.bar_geometry(2, 0, lo, hi, 0.99)
    fake = true.replace(f'y="{g_true["y"]}" width="{g_true["width"]}" height="{g_true["height"]}"',
                        f'y="{g_fake["y"]}" width="{g_fake["width"]}" height="{g_fake["height"]}"')
    assert fake != true  # the attack is in place
    script = f"open('figure.svg', 'w', newline='\\n').write({fake!r})\n"
    from custom_harness.figures import FigureError
    with pytest.raises(FigureError, match="geometry does not match"):
        _make(world, script)


def test_attack_fabricated_number_in_free_text(world):
    from custom_harness.figures import FigureError
    with pytest.raises(FigureError, match="free text carries a number"):
        _make(world, HONEST.replace("Held-out accuracy", "Held-out accuracy 0.99"))


def test_attack_unbound_mark(world):
    d, _ = _make(world)
    p = d / "figure.svg"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "</g>\n</svg>", '<rect class="mark" x="1.00" y="1.00" width="5.00" height="9.00"/>\n</g>\n</svg>'),
        encoding="utf-8", newline="\n")
    _reseal(d)
    res = _audit(world, d)
    assert not res.ok and any("not bound to a resolved ref" in x for x in res.problems), res.problems


def test_attack_hostile_svg_content_is_refused(world):
    from custom_harness.figures import check_svg
    good = (_make(world)[0] / "figure.svg").read_bytes()
    for bad in [b'<!DOCTYPE svg [<!ENTITY x "0.99">]>' + good,
                good.replace(b"</svg>", b"<script>alert(1)</script></svg>"),
                good.replace(b"</svg>", b"<path/></svg>"),  # an empty element: only the element whitelist sees it
                good.replace(b'class="mark"', b'class="mark" transform="scale(1,2)"', 1)]:
        assert check_svg(bad, {"ours": 0.91, "base": 0.62}), bad[:80]


# ------------------------------------------------------------------ attacks: swapped artifacts
def test_attack_swapped_artifact_in_figure_json(world):
    """After rendering, the 'ours' ref is pointed at the baseline's evidence (same shape, other number)."""
    d, meta_sha = _make(world)
    _, refs, _, _ = world
    meta = json.loads((d / "figure.json").read_bytes())
    meta["refs"]["ours"] = refs["ours2"]
    (d / "figure.json").write_bytes(json.dumps(meta, sort_keys=True, indent=1).encode())
    assert json.loads((d / "figure.json").read_bytes())["refs"]["ours"] == refs["ours2"]  # the attack is in place
    res = _audit(world, d, meta_sha256=meta_sha)
    assert not res.ok and "figure.json differs from the one recorded" in res.problems[0]
    res = _audit(world, d)  # even without the recorded hash, the plotted data no longer match the artifacts
    assert not res.ok and any("F3 mark ours" in x for x in res.problems), res.problems


def test_attack_evidence_outside_the_allowed_set(world):
    d, _ = _make(world)
    _, refs, _, _ = world
    allowed = {refs["base"]["evidence"]}
    res = _audit(world, d, allowed_evidence=allowed)
    assert not res.ok and any("not among the evidence this figure may plot" in x for x in res.problems)


def test_a_ref_to_a_raw_blob_is_not_evidence(world):
    from custom_harness.figures import FigureError, resolve_refs
    store, _, _, _ = world
    blob = store.blobs.put_json({"score": 0.99})
    with pytest.raises(FigureError, match="does not resolve"):
        resolve_refs({"x": {"evidence": blob, "pointer": "/score"}}, store, None)


# ------------------------------------------------------------------ script output does not match
def test_stored_figure_that_its_script_does_not_produce_fails_F4(world):
    """A valid figure of the same data with another title replaces the stored one (hashes resealed): every static
    check passes, only the re-render catches it."""
    d, _ = _make(world)
    other, _ = _make(world, HONEST.replace("Held-out accuracy", "Training accuracy"), name="other")
    (d / "figure.svg").write_bytes((other / "figure.svg").read_bytes())
    _reseal(d)
    assert b"Training accuracy" in (d / "figure.svg").read_bytes()  # the attack is in place
    res = _audit(world, d)
    assert not res.ok and res.problems == [
        "F4 the script's output on the resolved artifacts differs from the stored figure"], res.problems


def test_edited_script_is_caught(world):
    d, _ = _make(world)
    (d / "figure.py").write_text(HONEST.replace("accuracy", "acc"), encoding="utf-8", newline="\n")
    res = _audit(world, d)
    assert not res.ok and any("figure.py differs" in x for x in res.problems)


def test_audit_figures_state_helper(world):
    from custom_harness.figures import audit_figures
    store, _, box, _ = world
    assert audit_figures(None, store, lambda i: box(), None)["I6"] == "n/a"
    d, meta_sha = _make(world)
    assert audit_figures([{"dir": str(d), "meta_sha256": meta_sha}], store, lambda i: box(), None)["I6"] is True
    bad = audit_figures([{"dir": str(d), "meta_sha256": "0" * 64}], store, lambda i: box(), None)
    assert bad["I6"] is False and bad["problems"]
    assert audit_figures([{"dir": str(d)}], store, lambda i: box(), None)["I6"] is False
