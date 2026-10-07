"""Private values that leak in PART or ENCODED (STATUS §D: the engine scrubber and the audit matched whole values
verbatim only). Covered encodings: reversed, hex (lower/upper), base64 (standard and URL-safe, at any byte alignment
inside a longer encoded text), URL/percent encoding, case changes; plus long substrings (fragments) of high-entropy
secrets. Minimum lengths keep short values from causing false positives; the negative tests pin that an honest run
(and perfect predictions, see test_perfect_dev_predictions_are_not_a_leak) is never flagged."""

import base64
import random
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import pytest

from custom_harness.runtime import Edge, Engine, HarnessSpec, ModuleNode, register
from custom_harness.verify import leak
from custom_harness.verify.leak import leak_audit

CANARY = "CHH-CANARY-" + "3f9a2b7c4d1e8f6a0b5c9d2e7f1a4b8c"


def _encodings(secret: str) -> dict[str, str]:
    """Agent-visible texts that each carry `secret` in one transformed form, embedded in other text."""
    core = secret.removeprefix("CHH-CANARY-")
    return {
        "reversed": f"debug {secret[::-1]} end",
        "hex": f"blob={secret.encode().hex()};",
        "HEX": f"blob={secret.encode().hex().upper()};",
        "base64": "payload " + base64.b64encode(secret.encode()).decode(),
        "base64-embedded-offset1": base64.b64encode(b"x" + secret.encode() + b"tail").decode(),
        "base64-embedded-offset2": base64.b64encode(b"xy" + secret.encode() + b"tail").decode(),
        "base64-urlsafe": base64.urlsafe_b64encode(b"zz" + secret.encode()).decode(),
        "percent": "q=" + "".join(f"%{b:02X}" for b in secret.encode()),
        "url-quote": "path=" + urllib.parse.quote(secret + " /?", safe=""),
        "upper": f"note {secret.upper()} x",
        "fragment": f"partial {core[5:25]} partial",  # 20 characters of the 32-character random core
        "reversed-fragment": f"partial {core[::-1][3:22]} partial",
    }


# ---------------------------------------------------------------- engine scrubber
@register("test.leak_setup")
def _setup(state, params, ctx):
    return {"_private_secret": CANARY}


@register("test.leak_emit")
def _emit(state, params, ctx):
    return {"text": params["text"]}


def _run(store, text: str):
    spec = HarnessSpec(id="leakenc", entry="a", nodes={"a": ModuleNode(module="test.leak_setup"),
                                                       "b": ModuleNode(module="test.leak_emit", params={"text": text})},
                       edges=[Edge(source="a", target="b")])
    res = Engine(spec, backends={}, store=store).run("t", {})
    return store.trace(res.run_id)[1]


@pytest.mark.parametrize("kind", list(_encodings(CANARY)))
def test_engine_scrubber_catches_encoded_and_partial_private_values(store, kind):
    text = _encodings(CANARY)[kind]
    step = _run(store, text)
    assert step.output.get("_redacted_private_values"), (kind, step.output)  # the audit will fail closed on it
    assert step.output["text"] != text


def test_engine_scrubber_ignores_honest_text(store):
    """Negative control: other hashes, other base64, the public canary prefix, short fragments of the secret."""
    core = CANARY.removeprefix("CHH-CANARY-")
    honest = " ".join([secrets.token_hex(32), base64.b64encode(secrets.token_bytes(48)).decode(),
                       "CHH-CANARY-" + secrets.token_hex(16), core[:15], core[-15:], "the model scored 0.93"])
    step = _run(store, honest)
    assert "_redacted_private_values" not in step.output and step.output["text"] == honest


# ---------------------------------------------------------------- audit (I5, verify/leak.py; also used by K9)
@dataclass
class _Step:
    index: int
    node_id: str
    output: Any
    error: str | None = None
    extra: dict = field(default_factory=dict)


LABELS = [str(random.Random(i).randrange(3)) for i in range(120)]


@pytest.mark.parametrize("kind", list(_encodings(CANARY)))
def test_audit_catches_encoded_canary_in_trace_and_workspace(tmp_path, kind):
    text = _encodings(CANARY)[kind]
    fp = {"hashes": {}, "canaries": [], "label_windows": leak.label_windows(LABELS)}
    r = leak_audit([_Step(0, "n", {"t": text})], None, fp, [CANARY])
    assert not r["passed"] and any("canary" in p and "trace step 0" in p for p in r["problems"]), (kind, r)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text(text)
    r = leak_audit([], ws, fp, [CANARY])
    assert not r["passed"] and any("workspace file notes.txt" in p for p in r["problems"]), (kind, r)
    assert all(CANARY not in p for p in r["problems"])


@pytest.mark.parametrize("enc", ["reversed", "hex", "base64", "percent"])
def test_audit_catches_encoded_label_window_in_trace(enc):
    window = ",".join(LABELS[:40])
    text = {"reversed": window[::-1], "hex": window.encode().hex(),
            "base64": base64.b64encode(b"x" + window.encode()).decode(),
            "percent": urllib.parse.quote(window, safe="")}[enc]
    fp = {"hashes": {}, "canaries": [], "label_windows": leak.label_windows(LABELS)}
    r = leak_audit([_Step(0, "n", {"t": f"prompt text {text} more"})], None, fp, [])
    assert not r["passed"] and any("hidden test labels" in p for p in r["problems"]), (enc, r)


def test_audit_catches_a_private_file_embedded_in_a_larger_workspace_file(tmp_path):
    private = ("label\n" + "\n".join(LABELS) + "\n").encode()
    fp = {"hashes": {"h": "test/test_y.csv"}, "canaries": [], "label_windows": [], "contents": [private]}
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.bin").write_bytes(b"header junk\n" + private + b"trailer")
    r = leak_audit([], ws, fp, [])
    assert not r["passed"] and any("contains a copy of a private file" in p for p in r["problems"]), r


def test_audit_honest_run_negative_control(tmp_path):
    """Perfect predictions, other hashes/base64, the canary prefix, short label runs: never a leak."""
    fp = {"hashes": {}, "canaries": [], "label_windows": leak.label_windows(LABELS),
          "contents": [("label\n" + "\n".join(LABELS) + "\n").encode()]}
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "out").mkdir()
    (ws / "out" / "preds.csv").write_text("prediction\n" + "\n".join(LABELS) + "\n")  # a PERFECT predictions file
    (ws / "model.txt").write_text(base64.b64encode(secrets.token_bytes(4000)).decode() + secrets.token_hex(500))
    trace = [_Step(0, "n", {"t": "CHH-CANARY- is the prefix; hash " + secrets.token_hex(32),
                            "u": ",".join(LABELS[:39]), "v": base64.b64encode(",".join(LABELS[:39]).encode()).decode(),
                            "w": CANARY.removeprefix("CHH-CANARY-")[:15]})]
    r = leak_audit(trace, ws, fp, [CANARY])
    assert r["passed"], r


def test_short_values_get_no_encoded_or_fragment_matching():
    """Below the minimum lengths only the whole value is matched (short values would cause false positives)."""
    pats = leak.secret_patterns("ab12cd34ef")  # 10 characters: verbatim only
    assert pats == ["ab12cd34ef"]
    assert leak.secret_patterns("abc", min_verbatim=8) == []
    long_pats = leak.secret_patterns(CANARY)
    assert all(len(p) >= leak.FRAGMENT_LEN for p in long_pats)
