"""Independent replication of a certified Court case (GOAL §17.1 REPLICATED; invariant 5; GOAL L4.9).

    chh court replicate CASE --replicator KIND --beacon-round R [--allow-same-family]

The existing replication agent's rules (research/claims.py: the replicator sees the claim and the idea, NEVER the
code; a brief that carries original code lines is refused; a replica structurally similar to the original is not an
independent replication) applied to a Lab promotion:
  1. the case must be CERTIFIED (nothing to replicate otherwise);
  2. invariant 5: the replicator's model family must differ from the author's (the Lab agent that wrote the method,
     from its session record). policy.model_policy refuses a violation unless `--allow-same-family`; an override is
     RECORDED as "NOT MET" and the replica then never counts toward REPLICATED (court/levels.py). An author whose
     family cannot be determined is "unknown" and never counts as different (fail closed): with only one model
     family available, invariant 5 is NOT MET by construction;
  3. the brief (claim statement, the researcher's sentence, the minted family's PUBLIC description and columns, the
     contract) goes to the replicator (a gateway backend, role `replicator`); a replayed (cassette) answer is refused
     (invariant 21);
  4. the replica code becomes the method arm of a NEW minted Court case (same baseline, same minimum effect, same
     family, a fresh target minted after ITS freeze), linked to the original (`of_case`, `lab:...:replica:<case>`);
     it then goes through `chh court mint` / `approve-role` / `chh court run` like any case.
The model call happens on the Lab/host side, never in the Kernel (invariant 3); its output is evidence only after the
Kernel certifies the replica case.
Built, NOT test-verified.
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
from typing import Any

from ..audit import wrapper
from ..gateway import Backend
from ..gateway.base import ChatRequest, Message
from ..store import EvidenceStore
from . import minted as C

AGENT_FAMILIES = {"claude-code": "anthropic", "codex": "openai"}  # Lab agent adapters whose vendor fixes the family
_CODE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
SYSTEM = ("You are an independent replicator. You re-implement a method from its description alone: you have not "
          "seen and must not ask for the original code. Use the Python standard library only.")


class ReplicationRefused(RuntimeError):
    pass


def author_family(session: dict[str, Any]) -> str:
    """The model family of the Lab agent that authored the promoted method (policy.model_family rules)."""
    from ..policy import model_family
    model = str((session.get("options") or {}).get("model") or "")
    fam = model_family(SimpleNamespace(provider="", model=model)) if model else "unknown:/"
    if fam.startswith("unknown:"):
        fam = AGENT_FAMILIES.get(str(session.get("agent")), fam)
    return fam


def brief_for(c: dict[str, Any], claim: dict[str, Any], sentence: str) -> dict[str, str]:
    from ..research.suite import FAMILIES
    fam = FAMILIES[str(c["family"])]
    columns = [col for col in fam.header if col != fam.target]
    return {"claim": str(claim.get("statement", "")), "sentence": sentence,
            "task": f"{fam.title}. {fam.description} Feature columns: {', '.join(columns)}; label column: label; "
                    f"metric: {fam.metric}.",
            "contract": "Write ONE Python file that defines build_model(seed) returning an object with fit(X, y) and "
                        "predict(X): X is a list of rows of floats, y a list of label strings; predict returns one "
                        "label string per row. No file or network access; the harness does all input and output."}


def extract_code(text: str) -> str:
    m = _CODE.search(text)
    return (m.group(1) if m else text).strip() + "\n"


def replicate(store: EvidenceStore, case_id: str, replicator: Backend, *, beacon_round: str,
              allow_same_family: bool = False, dev_pairs: tuple[int, int, int, int] | None = None,
              n_test: int | None = None) -> dict[str, Any]:
    from ..kerneld.scope import ScopedKernel
    from ..lab.promote import PromoteRequest, _promotion
    from ..lab.session import started
    from ..policy import model_policy
    from ..research.claims import MAX_STRUCTURAL_SIMILARITY, copied_code_lines, structural_similarity
    c = C.case(store, case_id)
    if c.get("role") != "promotion":
        raise ReplicationRefused("only a promoted case is replicated (a replica of a replica is not followed)")
    o = C.latest(store, case_id, "outcome")
    if o is None or not str(o.get("label", "")).startswith("CERTIFIED"):
        raise ReplicationRefused(f"case {case_id} is not certified: nothing to replicate")
    session = started(store, str(c["session_id"]), allow_stopped=True)
    fam_author = author_family(session)
    policy = model_policy({"executor": SimpleNamespace(family=fam_author), "replicator": replicator},
                          allow_same_family=allow_same_family)  # PolicyError propagates: refused before any call
    req = PromoteRequest.model_validate(_promotion(store, str(c["promotion_id"]))[0]["request"])
    kernel = ScopedKernel(store)
    claim = kernel.certificate(str(o["cert_hash"])).claim
    arts = kernel.vault.artifacts()
    original = wrapper.arm_part(store.blobs.get(arts[claim["subject"]["artifact_id"]].hash))
    baseline = wrapper.arm_part(store.blobs.get(arts[claim["comparator"]["artifact_id"]].hash))
    if original is None or baseline is None:
        raise ReplicationRefused("the case's arms are not composed with the audit wrapper")
    brief = brief_for(c, claim, req.sentence)
    text = "\n".join(f"{k}: {v}" for k, v in brief.items())
    if copied_code_lines(original.decode("utf-8", "replace"), text) >= 2:
        raise ReplicationRefused("the replication brief carries lines of the original code: refused")
    resp = replicator.complete(ChatRequest(messages=[Message(role="system", content=SYSTEM),
                                                     Message(role="user", content=text)]))
    if resp.replayed:
        raise ReplicationRefused("a replayed (cassette) answer is never evidence (invariant 21)")
    code = extract_code(resp.text)
    similarity = structural_similarity(original.decode("utf-8", "replace"), code,
                                       baseline.decode("utf-8", "replace"))
    if similarity >= MAX_STRUCTURAL_SIMILARITY:
        raise ReplicationRefused(f"the replica is structurally similar to the original ({similarity:.2f} >= "
                                 f"{MAX_STRUCTURAL_SIMILARITY}): not an independent re-implementation")
    inv5 = "met" if policy["invariant_5"] == "met" else str(policy["invariant_5"])
    sizing = C.size_held_out(req.claim, dev_pairs=dev_pairs, n_test=n_test)
    return C.register_case(
        store, role="replica", promotion_id=str(c["promotion_id"]), session_id=str(c["session_id"]),
        title=f"independent replication of Court case {case_id}", sentence=req.sentence, part=req.claim,
        budget=req.budget, method=code.encode("utf-8"), baseline=baseline, lock=None, family=str(c["family"]),
        beacon_round=beacon_round, sizing=sizing, of_case=case_id,
        extra={"invariant_5": inv5, "families": policy["families"], "similarity": round(similarity, 4),
               "brief_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "replicator": resp.provenance.model_dump(mode="json"), "replicator_cost": resp.cost.model_dump(),
               "response_blob": store.blobs.put(resp.text.encode("utf-8"))})
