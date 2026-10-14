"""Pins for STATUS.md §D accepted limitations (GOAL §16.6; registry redteam/findings.yaml, status 'accepted').

Each pin is a strict xfail that asserts the SECURE behaviour, plus a positive control showing the attack really runs
today (the TEST-BLIND lesson). A fix turns the pin into an XPASS, which fails the gate until the pin is promoted
to a normal test. Limitations with no pin are listed in the registry with the reason no test can be written.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from custom_harness.kernel.claim import canonical_json
from custom_harness.kernel.ledger import Entry, Ledger, entry_hash
from custom_harness.research.claims import code_similarity, copied_code_lines, structural_similarity
from custom_harness.research.socratic import is_genuine_question
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from custom_harness.verify import leak

ORIGINAL = """import csv
def train(rows, k):
    best = None
    for row in rows:
        score = sum(float(v) for v in row[:-1]) / k
        if best is None or score > best:
            best = score
    return best
"""
RENAMED = ORIGINAL.replace("rows", "samples").replace("row", "sample").replace("best", "top")
PARAPHRASE = """import csv
def fit(samples, neighbours):
    values = [sum(map(float, s[:len(s) - 1])) / neighbours for s in samples]
    return max(values) if values else None
"""


@pytest.mark.xfail(strict=True, reason="STATUS §D: paraphrased (restructured) copies pass every copy detector")
def test_d_paraphrased_copy_is_detected():
    assert (copied_code_lines(ORIGINAL, PARAPHRASE) >= 2 or code_similarity(ORIGINAL, PARAPHRASE) >= 0.8
            or structural_similarity(ORIGINAL, PARAPHRASE) >= 0.8)


def test_d_paraphrase_control_renamed_copy_is_caught_and_paraphrase_is_not():
    """Correction to STATUS §D (verified 2026-10-07): a RENAMED copy is now caught (structural similarity 1.0);
    only a restructured paraphrase evades."""
    assert structural_similarity(ORIGINAL, RENAMED) >= 0.8
    assert copied_code_lines(ORIGINAL, PARAPHRASE) < 2 and code_similarity(ORIGINAL, PARAPHRASE) < 0.8
    assert structural_similarity(ORIGINAL, PARAPHRASE) < 0.8


LABELS = [str((i * 7) % 3) for i in range(120)]
REMAPPED = ",".join({"0": "a", "1": "b", "2": "c"}[y] for y in LABELS)


@pytest.mark.xfail(strict=True, reason="STATUS §D / docs/kernel.md §7: re-mapped labels are not detected by K9")
def test_d_remapped_label_run_is_a_leak():
    assert leak.scan_text(REMAPPED, "code", [], leak.label_windows(LABELS), [leak.label_runs(LABELS)])


def test_d_remapped_labels_control_plain_run_is_caught():
    plain = ",".join(LABELS)
    assert leak.scan_text(plain, "code", [], leak.label_windows(LABELS), [leak.label_runs(LABELS)])
    assert not leak.scan_text(REMAPPED, "code", [], leak.label_windows(LABELS), [leak.label_runs(LABELS)])


SMUGGLED = "Is a gradient-boosted model with depth 3 and learning rate 0.05 the natural choice here?"


@pytest.mark.xfail(strict=True, reason="STATUS §D: the Socratic filter is a heuristic; question-shaped directives pass")
def test_d_question_shaped_directive_is_dropped():
    assert not is_genuine_question(SMUGGLED)


def test_d_socratic_control_plain_directive_is_dropped():
    assert not is_genuine_question("Use a gradient-boosted model with depth 3, could that help?")
    assert is_genuine_question(SMUGGLED)  # the smuggled one really passes today


@pytest.mark.xfail(strict=True, reason="STATUS §D: under LocalSandbox (tests only) programs can read the host")
def test_d_local_sandbox_cannot_read_host_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    secret = tmp_path / "host_secret.txt"
    secret.write_text("HOST-ONLY", encoding="utf-8")
    sb = LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)
    res = sb.exec([sys.executable, "-c", f"print(open({str(secret)!r}).read())"], timeout_s=60)
    assert "HOST-ONLY" not in res.stdout


def _rewrite_chain(store: EvidenceStore, tmp: Path) -> Ledger:
    """A store writer (A4) rewrites one entry and re-hashes the whole chain consistently into a fresh store."""
    old = Ledger(store).entries()
    fresh = EvidenceStore(tmp / "forged")
    prev = "genesis"
    rows = []
    for e in old:
        body = {**e.body, "approver": "mallory"} if e.kind == "role_approval" else e.body
        text = canonical_json(body).decode()
        h = entry_hash(e.seq, e.kind, text, prev, e.at)
        rows.append(Entry(e.seq, e.kind, body, prev, h, e.at).row())
        prev = h
    fresh.ledger_import(rows)
    return Ledger(fresh)


@pytest.mark.xfail(strict=True, reason="STATUS §D: the ledger is tamper-evident, not tamper-proof (a consistent "
                   "whole-chain rewrite passes verify(); only an external anchor detects it)")
def test_d_consistent_chain_rewrite_is_detected_without_an_anchor(tmp_path):
    from custom_harness.kernel import DatasetRole, Kernel
    store = EvidenceStore(tmp_path / "s")
    k = Kernel(store)
    k.vault.register_dataset("ext", DatasetRole.EXTERNAL, public={"a.csv": b"x\n1\n"}, private={"b.csv": b"y\n1\n"})
    k.approve_role("ext", approver="alice")
    forged = _rewrite_chain(store, tmp_path)
    assert forged.entries("role_approval")[0].body["approver"] == "mallory"  # the rewrite really happened
    store.close()
    assert forged.verify() != []

