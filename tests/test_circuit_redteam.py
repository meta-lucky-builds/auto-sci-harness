"""Adapter red team of the `circuit` adapter (GOAL §15 "adapter red team"; invariants 7, 13, 14, 19, 21).

The red team tried to get a FALSE (or wider-than-proven) claim certified through the circuit adapter. Every success
is a regression test here; every refused attack not already covered by tests/test_circuit_adapter.py or the adapter's
validation evidence is pinned too, so it stays refused (invariant 19). Findings, outcomes and commits:
docs/redteam_circuit.md.

INVARIANT 5 NOT MET FOR THE RED TEAM: the adapter builder and this red team are both Claude (Anthropic) models, so the
red team is NOT of a different model family from the builder. These tests are evidence of what this red team tried,
not of an independent cross-family review.

Approval convention (as in tests/test_circuit_adapter.py): Kernel-path tests approve adapters only in a throwaway test
ledger with approver "test-world" (the repo's test stand-in for the human). Nothing here touches a real store.

Strict xfails (history): the holes whose fix lay in the Kernel (owner s3-kernel) were `strict=True` xfails asserting
the DESIRED behaviour. The Kernel closed them (K1 statement/scope rule 74f354b; canonical-UTF-8 lp reader and K9
scanning what a JSON reader sees d3feabd), they turned into XPASS failures, and the markers were removed (stage-c2,
2026-10-26). Each former xfail now asserts the specific check and closed reason code that refuses its attack, so it
fails again if that rule is removed (museum SCOPE-STATEMENT and K9-ENCODING patches; invariant 19).
"""

from __future__ import annotations

import ast
import hashlib
import json
import random
import time
from fractions import Fraction

import pytest

from custom_harness.adapters.circuit import check as C
from custom_harness.adapters.circuit import screen as CS
from custom_harness.adapters.circuit import solver as S
from custom_harness.adapters.circuit import validation as V
from custom_harness.kernel import (
    AnalysisPlan,
    ArtifactRef,
    Certificate,
    Claim,
    ClaimKind,
    Effect,
    EvidenceClass,
    Label,
    MetricRef,
    Scope,
)
from custom_harness.kernel import adapters as A
from custom_harness.kernel.claim import canonical_json
from kernel_world import World

DIV = V.problem(V.DIVIDER, V.node("n2"))
F = Fraction


def reasons(res) -> str:
    return " | ".join(res.reasons)


def isrc(eid: str, pos: str, neg: str, value: str) -> dict[str, str]:
    return {"id": eid, "type": "isource", "pos": pos, "neg": neg, "value": value, "unit": "A"}


def cert_bytes(problem: bytes, **witness) -> bytes:
    c = C.parse_problem(problem)
    return json.dumps({"format": C.CERT_FORMAT, "problem_sha256": c.sha256, "unknowns": list(c.unknowns),
                       **witness}).encode()


def both(problem: bytes, cert: bytes | dict, value) -> tuple[list[str], list[str]]:
    data = cert if isinstance(cert, bytes) else json.dumps(cert).encode()
    return C.check(problem, data, F(value)), C.bounds(problem, data, F(value))


# ------------------------------------------------------------------ Kernel path helpers (as in test_circuit_adapter)
@pytest.fixture()
def cw(tmp_path):
    w = World(tmp_path / "circuit-rt", seed=23, n_train=30, n_test=30)
    w.kernel.approve_adapter("circuit", approver="test-world")  # the test world's stand-in for the human approval
    yield w
    w.close()


def circuit_claim(w: World, problem: bytes = DIV, value: str | None = "6", statement: str | None = None,
                  evidence_class: str = "computational", **changes) -> Claim:
    aid = f"netlist-{hashlib.sha256(problem).hexdigest()[:10]}"
    ref = ArtifactRef(artifact_id=aid, hash=w.vault.register_artifact(aid, problem, "netlist").hash)
    try:
        phrase = C.scope_phrase(problem, Fraction(value or "0"))
    except ValueError:  # an unreadable netlist (the reader refuses it) has no scope phrase: callers pass one
        phrase = "unreadable netlist"
    doc = Claim(kind=ClaimKind.IMPROVEMENT, statement=statement if statement is not None else phrase, subject=ref,
                comparator=ref, metric=MetricRef(id=C.METRIC, higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=()), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    doc |= {"kind": "computed_value", "evidence_class": evidence_class, "value": value}
    for path, v in changes.items():
        node = doc
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = v
    return Claim.model_validate(doc)


def circuit_certify(w: World, claim: Claim, cert: bytes | None = None) -> Certificate:
    w.kernel.preregister(claim)
    data = cert if cert is not None else S.certify(w.store.blobs.get(claim.subject.hash))[1]
    aid = f"ccert-{claim.claim_hash()[:10]}-{hashlib.sha256(data).hexdigest()[:10]}"
    w.vault.register_artifact(aid, data, "circuit certificate")
    return w.kernel.certify(claim, [w.kernel.record_certificate(claim, aid)])


# ------------------------------------------------------------------ attack 4: scope creep to PHYSICAL (SUCCESS)
PHYSICAL_STATEMENTS = [
    "this circuit will work when built",
    "R2 is rated for 5 W and will not overheat in the built circuit",
    f"{C.scope_phrase(DIV, F(6))}; the same holds for the physical circuit when built",
]


@pytest.mark.parametrize("statement", PHYSICAL_STATEMENTS)
def test_a_physical_statement_on_a_true_circuit_value_is_refused_at_k1(cw, statement):
    """SUCCESSFUL ATTACK on fc24df8: a computational claim whose free-text statement is PHYSICAL certified
    CERTIFIED-EXACT, because k1_computational never compared the statement with the adapter's scope_phrase. Closed by
    the Kernel's K1 rule (74f354b, statement_reasons): REJECTED at K1 only, with K1_STATEMENT (the value, netlist and
    certificate are true, so no other check may be what refuses it)."""
    cert = circuit_certify(cw, circuit_claim(cw, statement=statement))
    assert cert.label == Label.REJECTED and cert.failed() == ["K1"], cert.failed()
    assert "K1_STATEMENT" in cert.checks["K1"].codes, cert.checks["K1"]
    assert "scope rendering" in reasons(cert.checks["K1"])


@pytest.mark.parametrize("conditions", [{"holds_in": "physical world"}, {"rating": "5 W"},
                                        {"model": "real breadboard"}])
def test_physical_scope_conditions_on_a_circuit_claim_are_refused_at_k1(cw, conditions):
    """SUCCESSFUL ATTACK on fc24df8: scope.conditions such as {"holds_in": "physical world"} rode along on a certified
    exact claim. Closed by the Kernel's K1 rule (74f354b): REJECTED at K1 only, with K1_SCOPE only. The statement is
    the adapter's own rendering, so the scope rule (not the statement rule) is what refuses it."""
    cert = circuit_certify(cw, circuit_claim(cw, scope__conditions=conditions))
    assert cert.label == Label.REJECTED and cert.failed() == ["K1"], cert.failed()
    assert cert.checks["K1"].codes == ("K1_SCOPE",), cert.checks["K1"]


def test_generalised_lp_claim_with_physical_statement_and_conditions_is_refused_at_k1(tmp_path):
    """The same hole in the EXISTING lp adapter (generalisation): an LP claim whose statement and scope conditions
    speak of a real factory certified CERTIFIED-EXACT on fc24df8. Closed by 74f354b: REJECTED at K1 only, with both
    K1_STATEMENT and K1_SCOPE (the optimum 13/5 and its certificate are true)."""
    w = World(tmp_path / "lp-rt", seed=11, n_train=30, n_test=30)
    try:
        w.kernel.approve_adapter("lp", approver="test-world")
        problem = canonical_json({"format": "chh-lp/1", "sense": "max", "c": ["1", "1"],
                                  "A": [["2", "1"], ["1", "3"]], "b": ["4", "5"]})
        cert = canonical_json({"format": "chh-lp-certificate/1", "x": ["7/5", "6/5"], "y": ["2/5", "1/5"]})
        ref = ArtifactRef(artifact_id="lp-rt", hash=w.vault.register_artifact("lp-rt", problem, "problem").hash)
        doc = Claim(kind=ClaimKind.IMPROVEMENT, statement="the real factory will earn 13/5 per day when run",
                    subject=ref, comparator=ref, metric=MetricRef(id="lp_optimum", higher_is_better=True),
                    effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                    scope=Scope(datasets=(), conditions={"holds_in": "physical world"}), development=(),
                    generalization_targets=(), analysis_plan=AnalysisPlan(seeds=()),
                    resolve_by=time.time() + 86400).model_dump(mode="json")
        doc |= {"kind": "computed_value", "evidence_class": "computational", "value": "13/5"}
        claim = Claim.model_validate(doc)
        w.kernel.preregister(claim)
        aid = w.vault.register_artifact("lp-rt-cert", cert, "certificate").artifact_id
        got = w.kernel.certify(claim, [w.kernel.record_certificate(claim, aid)])
        assert got.label == Label.REJECTED and got.failed() == ["K1"], got.failed()
        assert {"K1_STATEMENT", "K1_SCOPE"} <= set(got.checks["K1"].codes), got.checks["K1"]
    finally:
        w.close()


def test_the_circuit_query_vocabulary_cannot_express_a_physical_quantity():
    """Refused: the physical content cannot enter through the HASHED problem either (only the free text above can)."""
    for q in ("works_when_built", "power_rating", "max_temperature", "lifetime", "tolerance"):
        got = C.check(V.problem(V.DIVIDER, {"quantity": q, "unit": "1"}), b"{}", F(1))
        assert any("is not registered" in r for r in got), q


# ------------------------------------------------------------------ relabelling (refused)
def test_kind_metric_direction_and_comparator_relabels_are_refused(cw):
    kind = circuit_certify(cw, circuit_claim(cw, kind="improvement"))
    assert kind.label == Label.REJECTED and "K1" in kind.failed()
    direction = circuit_certify(cw, circuit_claim(cw, metric__higher_is_better=False))
    assert direction.label == Label.REJECTED and "direction does not match" in reasons(direction.checks["K1"])
    other = circuit_claim(cw, V.problem(V.DIVIDER, V.SINGULAR), "0")
    comp = circuit_certify(cw, circuit_claim(cw, comparator=other.subject.model_dump(mode="json")))
    assert comp.label == Label.REJECTED and "comparator" in reasons(comp.checks["K1"])


def test_relabelling_the_metric_to_another_adapter_routes_the_netlist_to_a_checker_that_refuses_it(cw):
    """metric `lp_optimum` on a circuit netlist: the lp adapter (unapproved here) is resolved, never the circuit one;
    once lp is approved too, its checker refuses the netlist (wrong format), so nothing certifies."""
    cert = circuit_certify(cw, circuit_claim(cw, metric__id="lp_optimum"))
    assert cert.label == Label.REJECTED and "adapter 'lp'" in reasons(cert.checks["K1"])
    cw.kernel.approve_adapter("lp", approver="test-world")
    cert = circuit_certify(cw, circuit_claim(cw, metric__id="lp_optimum", statement="lp relabel"))
    assert cert.label == Label.REJECTED and "K3" in cert.failed() and cert.adapters[0]["name"] == "lp"


# ------------------------------------------------------------------ attack 7: approval and identity (refused)
def test_screen_is_outside_the_adapter_hash_and_cannot_affect_certification(cw, monkeypatch):
    """screen.py is intentionally NOT covered by the circuit adapter hash. Nothing on the Kernel path may import it:
    the checker's imports do not reach it, and a sabotaged screen changes nothing."""
    spec, _ = A.REGISTRY["circuit"]
    covered = A.covered_modules(spec)
    assert "custom_harness.adapters.circuit.screen" not in covered
    for mod, path in covered.items():
        names = A._imports(mod, path, path.read_bytes())
        assert "custom_harness.adapters.circuit.screen" not in names, mod

    def boom(*a, **k):
        raise AssertionError("screen reached from the Kernel path")

    monkeypatch.setattr(CS, "float_screen", boom)
    monkeypatch.setattr(CS, "exact", boom)
    assert circuit_certify(cw, circuit_claim(cw)).label == Label.CERTIFIED_EXACT
    assert circuit_certify(cw, circuit_claim(cw, value="7")).label == Label.REJECTED


def test_the_checker_module_imports_only_the_stdlib():
    """The trusted checker's behaviour is pinned by its own bytes plus the stdlib: no project import it could inherit
    an unhashed change from (covered_modules would catch one, but there should be none)."""
    tree = ast.parse(open(C.__file__, "rb").read())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {str(n.module) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= {"__future__", "hashlib", "json", "re", "dataclasses", "fractions", "typing"}, imported


def test_an_unapproved_second_adapter_serving_circuit_value_does_not_hijack_resolution(cw, monkeypatch):
    """A rogue registry entry that sorts BEFORE 'circuit' and serves the same (class, metric) is skipped while it has
    no approval; the approved circuit adapter is still the one resolved."""
    rogue = A.AdapterSpec("aaa_rogue", "1", frozenset({EvidenceClass.COMPUTATIONAL}), ("custom_harness.kernel.lp",),
                          frozenset({C.METRIC}), "custom_harness.kernel.lp")
    monkeypatch.setitem(A.REGISTRY, "aaa_rogue", (rogue, A.LP_CHECKS))
    adapter, why = A.resolve(cw.kernel.ledger, EvidenceClass.COMPUTATIONAL, C.METRIC)
    assert adapter is not None and adapter["name"] == "circuit" and why == []
    cert = circuit_certify(cw, circuit_claim(cw))
    assert cert.label == Label.CERTIFIED_EXACT and cert.adapters[0]["name"] == "circuit"


# ------------------------------------------------------------------ attack 6: K8 independence (stamps vs laws)
def _random_netlist(rng: random.Random) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    nodes = ("0", *(f"n{i}" for i in range(rng.randint(1, 5))))
    pick = lambda: rng.choice(nodes)  # noqa: E731
    val = lambda: str(rng.choice([-3, -2, -1, 1, 2, 3, 5])) + rng.choice(["", "/2", "/3"])  # noqa: E731
    els: list[dict[str, str]] = [V.vs("V0", pick(), pick(), val())]
    for i in range(rng.randint(1, 7)):
        kind = rng.choice(["resistor", "vsource", "isource", "vcvs", "vccs", "ccvs", "cccs"])
        e = {"id": f"E{i}", "type": kind, "pos": pick(), "neg": pick(), "value": val(),
             "unit": {"resistor": "ohm", "vsource": "V", "isource": "A", "vcvs": "V/V", "vccs": "S", "ccvs": "ohm",
                      "cccs": "A/A"}[kind]}
        if kind == "resistor":
            e["value"] = str(rng.randint(1, 9))
        if kind in ("vcvs", "vccs"):
            e |= {"ctrl_pos": pick(), "ctrl_neg": pick()}
        if kind in ("ccvs", "cccs"):
            e["control"] = "V0"
        els.append(e)
    return nodes, els


def test_mna_rows_and_element_laws_agree_on_every_vector():
    """K8 is a second formulation (element by element) of the same model as the K3 stamps. If they disagreed on any
    x, either a wrong solution would pass K3 (stamping bug) or K8 would reject a true one. Attack: search random
    netlists (all seven element types, self-loops, floating nodes, singular and inconsistent ones) for a vector on
    which `A x = b` and the element laws disagree. None found at fc24df8; this pins the equivalence."""
    rng = random.Random(20261009)
    tried = solutions = 0
    for _ in range(300):
        nodes, els = _random_netlist(rng)
        p = V.problem(els, V.INCONSISTENT, nodes)
        c = C.parse_problem(p)
        a, b = C.mna(c)
        n = len(c.unknowns)
        vectors = [[F(rng.randint(-3, 3), rng.randint(1, 2)) for _ in range(n)] for _ in range(3)]
        red = S.reduce(a, b)
        consistent = all(red.rhs[i] == 0 for i in range(len(red.pivots), n))
        if consistent:  # a particular solution, and that solution shifted along a null vector when one exists
            x = [F(0)] * n
            for row, col in enumerate(red.pivots):
                x[col] = red.rhs[row]
            vectors.append(x)
            vectors += [[xi + d for xi, d in zip(x, e_k, strict=True)] for e_k in
                         ([F(int(j == k)) for j in range(n)] for k in range(n))]
            free = [j for j in range(n) if j not in red.pivots]
            if free:
                f = free[0]
                null = [F(0)] * n
                null[f] = F(1)
                for row, col in enumerate(red.pivots):
                    null[col] = -red.rref[row][f]
                vectors.append([xi + 7 * v for xi, v in zip(x, null, strict=True)])
        for x in vectors:
            mna_ok = all(sum((a[i][j] * x[j] for j in range(n)), F(0)) == b[i] for i in range(n))
            laws_ok = C.kirchhoff(c, x) == []
            assert mna_ok == laws_ok, (p, x)
            tried += 1
            solutions += mna_ok
    assert tried > 1000 and solutions > 100, (tried, solutions)


# ------------------------------------------------------------------ attack 1: canaries smuggled past K9 (SUCCESS)
def _canary(w: World) -> str:
    tokens = [t for t in (w.vault.canary(d) for d in w.vault.datasets()) if t]
    assert tokens
    return tokens[0]


def _smuggle(doc_bytes: bytes, token: str, how: str) -> bytes:
    doc = json.loads(doc_bytes) | {"comment": f"x {token} y"}
    if how in ("utf-16", "utf-32"):
        return json.dumps(doc).encode(how)
    assert how == "escaped"
    text = json.dumps(doc).replace(token, "\\u0043" + token[1:])  # JSON reads it back as the token
    assert json.loads(text)["comment"] == f"x {token} y" and token not in text
    return text.encode()


@pytest.mark.parametrize("how", ["utf-16", "utf-32", "escaped"])
def test_a_canary_hidden_in_a_certificate_comment_is_not_certified(cw, how):
    """SUCCESSFUL ATTACK (fc24df8): the circuit reader accepts every encoding json.loads auto-detects (UTF-16, UTF-32,
    BOMs) and \\u escapes in the free `comment`, while K9 scans the raw bytes decoded as UTF-8: a held-out canary
    inside the certificate passes K9 and the claim certifies CERTIFIED-EXACT. Desired: REJECTED. (The adapter reading
    only plain UTF-8 without BOM and refusing escapes in `comment`, or no comment at all, closes it for circuit; a K9
    that scans the parsed strings would close it for every adapter.)"""
    token = _canary(cw)
    data = _smuggle(S.certify(DIV)[1], token, how)
    cert = circuit_certify(cw, circuit_claim(cw), data)
    assert cert.label == Label.REJECTED, cert.failed()
    # both layers, each required (stage-c2): the adapter's reader (dec7562; K3) and K9 scanning what a reader sees
    # (d3feabd; K9_LEAK), so removing either one fails this test
    assert "K9_LEAK" in cert.checks["K9"].codes, cert.checks["K9"]
    why = "backslash escapes are refused" if how == "escaped" else "not plain UTF-8"
    assert why in reasons(cert.checks["K3"]), cert.checks["K3"]


@pytest.mark.parametrize("how", ["utf-16", "escaped"])
def test_a_canary_hidden_in_the_netlist_comment_is_not_certified(cw, how):
    """The same smuggling through the PROBLEM (the netlist's `comment`), which K9 scanned as UTF-8 bytes on fc24df8.
    Two layers refuse it now, and both are required here: the circuit reader (dec7562: plain UTF-8, no escapes; K3)
    and K9, which scans what a reader sees (d3feabd; K9_LEAK). The attacker states the plain netlist's phrase and
    supplies the plain netlist's (true) certificate bound to the smuggled bytes' hash: the solver of the fixed reader
    refuses to build one, so the attacker brings what the old reader would have produced."""
    token = _canary(cw)
    problem = _smuggle(DIV, token, how)
    witness = json.loads(S.certify(DIV)[1]) | {"problem_sha256": hashlib.sha256(problem).hexdigest()}
    cert = circuit_certify(cw, circuit_claim(cw, problem, statement=C.scope_phrase(DIV, F(6))),
                           json.dumps(witness).encode())
    assert cert.label == Label.REJECTED, cert.failed()
    assert "K9_LEAK" in cert.checks["K9"].codes, cert.checks["K9"]
    why = "not plain UTF-8" if how == "utf-16" else "backslash escapes are refused"
    assert why in reasons(cert.checks["K3"]), cert.checks["K3"]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-le", "utf-32"])
def test_certificates_must_be_plain_utf8_without_bom(encoding):
    """The checker should read one byte encoding only, so that a reader (or K9) shown the bytes sees the document the
    parser sees. FAILS on fc24df8: all four are accepted."""
    _, cert = V.solved(DIV)
    k3, k8 = both(DIV, json.dumps(cert).encode(encoding), 6)
    assert k3 and k8, encoding


def test_generalised_lp_reader_refuses_utf16():
    """The same class of hole in the lp adapter's reader (it called json.loads on raw bytes). Closed by d3feabd: the lp
    reader decodes canonical UTF-8 only. The plain UTF-8 spelling of the same certificate is accepted (control), so
    the refusal is the encoding's."""
    from custom_harness.kernel import lp
    p = {"format": "chh-lp/1", "sense": "max", "c": ["1", "1"], "A": [["2", "1"], ["1", "3"]], "b": ["4", "5"]}
    c = {"format": "chh-lp-certificate/1", "x": ["7/5", "6/5"], "y": ["2/5", "1/5"], "comment": "x"}
    assert lp.check(canonical_json(p), json.dumps(c).encode("utf-8"), F(13, 5)) == []
    got = lp.check(canonical_json(p), json.dumps(c).encode("utf-16"), F(13, 5))
    assert any("utf-8" in r for r in got), got


# ------------------------------------------------------------------ attack 1: number and JSON tricks (refused)
@pytest.mark.parametrize("text", ["02", "1/02", "+1", " 1", "6 ", "١", "６", "−6", "6.",
                                  "6e0", "0x6", "6/0", "-", "", "1//2", "6/-1", "-0/1/1"])
def test_non_canonical_number_spellings_are_refused(text):
    """Written as literal UTF-8: since dec7562 the reader refuses every backslash escape BEFORE the number check, so
    json.dumps' default escapes of the non-ASCII digits would never reach it (the test would not attack)."""
    _, cert = V.solved(DIV)
    cert["x"][1] = text
    data = json.dumps(cert, ensure_ascii=False).encode("utf-8")
    assert b"\\" not in data
    k3, k8 = both(DIV, data, 6)
    assert any("exact rational string" in r for r in k3), (text, k3)
    assert any("exact rational string" in r for r in k8), (text, k8)


def test_a_number_with_a_newline_is_refused_before_and_by_the_number_check():
    """'6' + newline has no literal spelling in strict JSON (a raw control character in a string is not JSON) and its
    escaped spelling is refused by the reader (dec7562), so in a document it can no longer reach the number check:
    both spellings are refused by the reader, each for its own reason. The number check itself still refuses it."""
    _, cert = V.solved(DIV)
    cert["x"][1] = "6\n"
    escaped = json.dumps(cert).encode()
    literal = escaped.replace(b'"6\\n"', b'"6\n"')
    assert escaped.count(b'"6\\n"') == 1 and literal != escaped
    for got in both(DIV, escaped, 6):
        assert any("backslash escapes are refused" in x for x in got), got
    for got in both(DIV, literal, 6):
        assert any("not JSON" in x and "control character" in x for x in got), got
    with pytest.raises(C.CircuitError, match="exact rational string"):
        C.parse_rational("6\n")


@pytest.mark.parametrize("value", [1e400, float("nan"), True, None, 6, ["6"], {"p": "6"}])
def test_non_string_numbers_are_refused(value):
    _, cert = V.solved(DIV)
    cert["x"][1] = value
    k3, k8 = both(DIV, json.dumps(cert).encode(), 6)  # 1e400 and nan serialise as Infinity / NaN
    assert k3 and k8 and all("unreadable" in r for r in k3), (value, k3)


def test_equal_value_spellings_in_a_certificate_are_accepted_and_harmless():
    """'12/2', '6/1' and '-0' read as their values: accepted where the value is right ('-0' for 6 is refused as a
    wrong value), and they change nothing the claim says (the claimed value itself must be canonical, K1)."""
    _, cert = V.solved(DIV)
    for s in ("12/2", "6/1"):
        cert["x"][1] = s
        assert both(DIV, cert, 6) == ([], [])
    cert["x"][1] = "-0"
    assert any("A x != b" in r for r in both(DIV, cert, 6)[0])


@pytest.mark.parametrize(("edit", "why"), [
    (lambda s: s.replace('"id": "R1",', '"id": "R1", "id": "R9",'), "duplicate key"),
    (lambda s: s.replace('"unit": "V"}', '"unit": "V", "Unit": "A"}', 1), "unexpected keys"),
    (lambda s: s.replace('"id": "R1"', '"ｉd": "R1", "id": "R1"'), "unexpected keys"),
    (lambda s: s.replace('"n2"', '"п2"'), "is not a name"),
    (lambda s: s.replace('"n2"', '"n2​"'), "is not a name"),
    (lambda s: s + " //x", "not JSON"),
    (lambda s: s.replace('"format"', '"comment": {"a": 1}, "format"', 1), "'comment' must be a string"),
    (lambda s: s.replace('"10"', "Infinity"), "JSON constant Infinity"),
    (lambda s: s.replace('"10"', "1e400"), "exact rational string"),
])
def test_json_tricks_in_the_netlist_are_refused(edit, why):
    problem = edit(DIV.decode()).encode("utf-8")
    k3, k8 = both(problem, b"{}", 6)
    assert any(why in r for r in k3), (why, k3)
    assert any(why in r for r in k8), (why, k8)


def test_a_byte_different_spelling_of_the_same_netlist_is_refused():
    """A JSON-escaped spelling of the same netlist (other bytes, same parse) was accepted under its own hash on
    fc24df8. Since the encoding fix the reader refuses every backslash escape, so only the literal spelling exists."""
    other = DIV.decode().replace('"n2"', '"n\\u0032"').encode()
    assert json.loads(other) == json.loads(DIV) and other != DIV
    _, cert = V.solved(DIV)
    k3, k8 = both(other, {**cert, "problem_sha256": hashlib.sha256(other).hexdigest()}, 6)
    assert any("backslash escapes are refused" in r for r in k3) and any("backslash" in r for r in k8)


# ------------------------------------------------------------------ attack 1: forged witnesses (refused)
def test_forged_witnesses_are_refused():
    _, cert = V.solved(DIV)
    k3, k8 = both(DIV, {**cert, "x": ["10", "6", "0"]}, 6)  # node voltages right, branch current wrong
    assert any("A x != b" in r for r in k3) and any("KCL violated" in r for r in k8)
    wide = {**cert, "inverse": [[*row, "0"] for row in cert["inverse"]]}  # a 3x4 "inverse"
    assert any("dimension mismatch" in r for r in both(DIV, wide, 6)[0])
    pe = V.problem(V.DIVIDER, V.INCONSISTENT)
    k3, _ = both(pe, cert_bytes(pe, left_null=["0", "0", "1"]), 1)  # y^T b != 0 but y^T A != 0
    assert any("y^T A != 0" in r for r in k3)
    k3, _ = both(V.problem(V.DIVIDER, V.SINGULAR), json.dumps(cert).encode(), 0)  # certificate of another query
    assert any("unexpected keys" in r or "certificate is for problem" in r for r in k3)


def test_huge_and_odd_null_vectors_of_a_truly_singular_network_are_fine():
    """A true null vector scaled by a 100-digit rational is still a null vector: accepted, and the claim (singular)
    is true. The same vector with one entry off is refused."""
    ps = V.problem(V.PARALLEL_EQUAL, V.SINGULAR, ("0", "n1"))
    big = "9" * 100
    assert both(ps, cert_bytes(ps, null_vector=["0", f"{big}/7", f"-{big}/7"]), 1) == ([], [])
    assert both(ps, cert_bytes(ps, null_vector=["0", f"{big}/7", f"-{big}1/7"]), 1)[0]


# ------------------------------------------------------------------ attack 2: singular networks passed as regular
SINGULAR_VALUE_CASES = {
    # name: (elements, nodes, query node); every MNA matrix here is singular
    "floating resistor island": ([*V.DIVIDER, V.r("R9", "n3", "n4", "1")], ("0", "n1", "n2", "n3", "n4"), "n2"),
    "parallel equal sources (voltage-source loop)": (V.PARALLEL_EQUAL, ("0", "n1"), "n1"),
    "current-source cutset": ([isrc("I1", "0", "n1", "1"), isrc("I2", "n1", "0", "1"), V.r("R1", "n2", "0", "1")],
                              ("0", "n1", "n2"), "n1"),
    "vcvs of gain 1 across its own control": (
        [V.vs("V1", "n1", "0", "1"), V.r("R1", "n1", "n2", "1"),
         {"id": "E1", "type": "vcvs", "pos": "n2", "neg": "0", "ctrl_pos": "n2", "ctrl_neg": "0", "value": "1",
          "unit": "V/V"}], ("0", "n1", "n2"), "n2"),
    "ground touches no element": ([V.vs("V1", "n1", "n2", "1"), V.r("R1", "n1", "n2", "1")], ("0", "n1", "n2"),
                                  "n1"),
    "0 V source from a node to itself": ([*V.DIVIDER, V.vs("V9", "n1", "n1", "0")], ("0", "n1", "n2"), "n2"),
}


@pytest.mark.parametrize("name", sorted(SINGULAR_VALUE_CASES))
def test_a_value_claim_on_a_singular_network_cannot_be_certified(name):
    """Attack: an honest solution x (A x = b holds) plus the solver's partial inverse, or the identity, for a value
    claim on a singular network. M A = I is impossible for a singular A, so each is refused, even where the queried
    value happens to be determined (floating island: V(n2) = 6). That is an incompleteness of the certificate format,
    not a soundness hole. The singular = 1 claim on the same network does certify."""
    els, nodes, n = SINGULAR_VALUE_CASES[name]
    p = V.problem(els, V.node(n), nodes)
    c = C.parse_problem(p)
    a, b = C.mna(c)
    red = S.reduce(a, b)
    k = len(c.unknowns)
    assert len(red.pivots) < k, name
    with pytest.raises(S.SolverError, match="singular"):
        S.certify(p)
    x = [F(0)] * k
    for i, col in enumerate(red.pivots):
        x[col] = red.rhs[i]
    for inv in (red.ops, [[F(int(i == j)) for j in range(k)] for i in range(k)]):
        doc = {"x": [str(v) for v in x], "inverse": [[str(v) for v in row] for row in inv]}
        k3, _ = both(p, cert_bytes(p, **doc), C.quantity(c, x))
        assert any("not a left inverse" in r for r in k3), (name, k3)
    sing = V.problem(els, V.SINGULAR, nodes)
    value, cert = S.certify(sing)
    assert value == 1 and both(sing, cert, 1) == ([], [])


def test_ground_handling():
    """The ground is whichever node `ground` names; a node merely CALLED "0" is then an ordinary node, and its voltage
    is relative to the declared ground (the netlist hash fixes the meaning). Absent, non-string, empty or duplicated
    grounds are refused."""
    doc = json.loads(DIV) | {"ground": "n2", "query": {"quantity": "node_voltage", "node": "0", "unit": "V"}}
    p = json.dumps(doc).encode()
    value, cert = S.certify(p)
    assert value == -6 and both(p, cert, -6) == ([], []) and both(p, cert, 6)[0]
    for bad in ({"ground": "g"}, {"ground": ["0", "n1"]}, {"ground": ""}):
        assert C.check(json.dumps(json.loads(DIV) | bad).encode(), b"{}", F(6))
    dup = DIV.replace(b'"ground": "0"', b'"ground": "0", "ground": "n2"')
    assert any("duplicate key" in r for r in C.check(dup, b"{}", F(6)))


# ------------------------------------------------------------------ attack 3: units (refused, one cosmetic gap)
@pytest.mark.parametrize("unit", ["kohm", "mV", "mA", "MΩ", "mΩ", "Ω", "Ohm", "OHM", "v", "ohm ",
                                  "Ω"])
def test_prefixed_and_respelled_units_are_refused(unit):
    """Written as literal UTF-8: V.problem's json.dumps would escape the ohm signs, and since dec7562 the reader
    refuses every escape first, so the unit check would never be reached (the test would not attack)."""
    p = V.problem([V.vs("V1", "n1", "0", "10"), V.r("R1", "n1", "n2", "2", unit), V.r("R2", "n2", "0", "3")],
                  V.node("n2"))
    p = json.dumps(json.loads(p), ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert b"\\" not in p
    assert any("not a registered SI unit" in r for r in C.check(p, b"{}", F(6)))
    assert any("not a registered SI unit" in r for r in C.bounds(p, b"{}", F(6)))


def test_unit_quantity_mismatches_are_refused_at_k8():
    for q, unit in (("node_voltage", "A"), ("node_voltage", "W"), ("node_voltage", "1"), ("branch_current", "V")):
        key = "node" if q == "node_voltage" else "element"
        p = V.problem(V.DIVIDER, {"quantity": q, key: "n2" if key == "node" else "R1", "unit": unit})
        value, cert = S.certify(p)
        assert any("does not have the dimension" in r for r in C.bounds(p, cert, value)), (q, unit)
    gain = [V.vs("V1", "n1", "0", "1"), V.r("R1", "n1", "0", "1"),
            {"id": "F1", "type": "cccs", "pos": "n2", "neg": "0", "control": "V1", "value": "2", "unit": "S"},
            V.r("R2", "n2", "0", "1")]
    p = V.problem(gain, V.node("n2"))
    value, cert = S.certify(p)
    assert any("element equation needs" in r for r in C.bounds(p, cert, value))


def test_dimensionless_spellings_are_interchangeable():
    """Observed, cosmetic (not a false claim): '1', 'V/V' and 'A/A' all have dimension V^0 A^0, so a CCCS gain may be
    written 'V/V' and a boolean query 'A/A'; the scope phrase then prints that unit. Pinned so a change is noticed."""
    p = V.problem(V.DIVIDER, {"quantity": "singular", "unit": "A/A"})
    value, cert = S.certify(p)
    assert value == 0 and both(p, cert, 0) == ([], [])
    assert "= 0 A/A exactly" in C.scope_phrase(p, F(0))
