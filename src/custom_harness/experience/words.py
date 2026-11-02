"""The researcher's vocabulary (GOAL 18.3, UPGRADES N16, N18): every sentence a main view shows is written here.

Six concepts reach a main view: Project, Lab, Case, Verdict and Badges, Story, Record. Everything else stays behind the
technical view. `jargon_problems` is the jargon budget as a check: tests walk every main view of a full session
through it. The words of a badge never decide it: `court/levels.py` decides, and these functions only read its answer.
"""

from __future__ import annotations

import re
import time
from typing import Any

# internal terms that may not appear on a main view. Upper-case words are matched as written, the rest in any case.
_JARGON = re.compile(
    r"\b(?:K[0-9]|T[0-6]|VG[0-9]?|MECHANICAL|CERTIFIED-[A-Z]+|SUPPORTED|REJECTED)\b"
    r"|(?i:\b(?:kerneld|kernel|ledger|alpha|hoststore|prereg\w*|pre-regist\w*|claim_hash|cert_hash|held-out|heldout"
    r"|minted?|beacon|vault|loop label|tier|autopsy|bundle)\b)")
# fields that hold no sentence of ours: identifiers, links, commands and the researcher's own words
NOT_SENTENCES = frozenset({"case_id", "promotion_id", "session_id", "project", "link", "id", "command", "researcher",
                           "os_user", "title", "sentence", "name", "idea_id", "reason", "reply", "path", "family",
                           "agent", "workspace", "rerun", "text", "query", "audit_id", "event_id", "of_case", "kind",
                           "idea_path"})

VERDICTS = {"CERTIFIED": "Certified.", "NOT CERTIFIED": "Not certified.", "INCONCLUSIVE": "Inconclusive.",
            "COULD NOT RUN": "The trial could not run."}
TOO_CLOSE = "TOO_CLOSE_TO_CALL"  # court/display.py
REASONS = {
    "NOT_REPRODUCED": "The result did not repeat when the frozen code was run again.",
    "PROCEDURE_NOT_VALID": "The test could not count as a valid test of this claim.",
    "EFFECT_NOT_SHOWN": "The claimed effect was not shown on the fresh test data.",
    "OUTCOME_TOO_PREDICTABLE": "The claim was too likely to come true by itself to count as a test.",
    "LEAK_FOUND": "Signs of a leak of the answers were found in the run.",
    "NOT_FIXED_FIRST": "The claim was not cleanly fixed before the test data was read.",
    "RULES_OF_THE_RUN_BROKEN": "A file or an output of the run broke the rules of the test.",
    TOO_CLOSE: "The test could not tell: the effect may or may not reach the claimed size.",
    "FAILED_INDEPENDENT_CHECK": "One of the checks shown in full failed: it is named in the technical details.",
}
CHECK_PLAIN = {"K0": "The code and records are all present", "K1": "The claim is well formed",
               "K2": "The claim was fixed before the data existed or was read",
               "K3": "The result repeats when the code is run again", "K4": "The test procedure is valid",
               "K5": "The right code and measure ran", "K6": "The claimed effect shows on the test data",
               "K7": "The claim was not too easy to come true", "K8": "Only permitted files were used",
               "K9": "No leak of the answers was found"}
SECOND_TEST = {"declared": "A second fresh test is fixed and has not finished.",
               "passed": "The second fresh test passed.",
               "failed": "Confirmed once; the second fresh test failed.",
               "invalid": "The second test does not count: see the technical details."}
FAILED_AFTER_READ = "It failed after it had begun reading the answers, so this look is spent."
RETRY = "Nothing was spent. Fix the problem and run the trial again."
CALIBRATION = "How close your stated chances were to what happened, on verdicts you opened."
PUBLIC_NEXT = "A person reviews the claim first. This route uses public data and cannot become a finding."
PROMOTE_STEPS = {
    "generated": ("The claim and the code are fixed for the Court.",
                  "After that, a public random value fixes fresh test data that did not exist before.",
                  "A person confirms the test data, then the frozen code is tried offline.",
                  "The answer can be a finding, about this data generator only."),
    "sealed": ("The claim and the code are fixed for the Court.",
               "The first sealed part becomes the test. It takes one look.",
               "A person confirms the test data, then the frozen code is tried offline.",
               "The answer can be a finding."),
    "future": ("The claim and the code are fixed for the Court.",
               "You wait for data that does not exist yet: this can take days or months.",
               "When it exists, you hand it over. A person confirms it is what it claims to be.",
               "The frozen code is tried offline. The answer can be a finding, for the time window you named."),
    "public": ("The claim and the code are fixed for the Court.",
               "A person reviews the claim, then it is tested on public data.",
               "The answer cannot be a finding: the Lab could have seen this data.")}
RESTS_ON = {"case": "What was fixed for this case before any answer was read.",
            "audit": "One audit is one look. Its test data is public, so it has no sealed count of rows here.",
            "seal": "The record of the seal, written by the Court before any Lab session.",
            "session": "The session's Story.", "promotion": "The frozen list of the workspace's files.",
            "rehearsal": "The last rehearsal, on Lab data.",
            "readiness": "The last readiness estimate, on Lab numbers.",
            "target": "The test data's remaining looks.", "viewer": "The record of what this person opened.",
            "record": "Verdicts this person opened.", "paper": "The paper's list of numbers.",
            "waiting": "The waiting list.", "cases": "This researcher's cases.",
            "sessions": "This researcher's Lab sessions.", "frozen": "The frozen ideas of this researcher.",
            "trust": "The checks of this machine, made just now.",
            "prior": "A pinned model's chance for this outcome, from the claim's words alone, before any test data."}
BADGES = {
    "REPRODUCIBLE": "Run again from the frozen code, offline, with the same result on every seed. One machine only: "
                    "other machines are not checked yet. Reproducible does not mean true.",
    "REPLICATED": "A different agent rebuilt it from the claim's words alone, without the code, and agreed.",
    "VALIDATED": "Confirmed on fresh test data that did not exist or was sealed while the Lab worked. This is a "
                 "finding.",
    "RE-VALIDATED": "Confirmed a second time on a second, separate set of fresh test data, chosen before the first "
                    "result, with the claim and the code unchanged.",
}
RETROSPECTIVE = "Tested on a public dataset the Lab could have downloaded. It cannot become a finding."
NOT_A_FINDING = "Certified, but not on fresh test data, so it is not a finding."
SAME_FAMILY = "Same model family: errors may be shared."
UNKNOWN_FAMILY = "Model family unknown: errors may be shared."
REPLICATED_RISK = "Replicas can share mistakes, so this is weaker than a fresh test."
LAB_NOTICE = "Lab work is exploration. It is not a finding."
REHEARSAL_NOTICE = "Lab data, not evidence. A rehearsal can look better than fresh data: the Lab may have tuned on it."
READINESS_NOTICE = "A rough estimate from Lab numbers, not calibrated. It can warn; it never promises."
IMPROVEMENT = "Nothing measured yet. This appears once real campaigns have measured evidence."
HUMAN_ONLY = "Only a person may do this, from a terminal. This doorway lists it and points to it; it never does it."
STEPS = {"needs_beacon": "The Court is waiting for the public random value of the round you chose.",
         "needs_data": "The Court is waiting for the future data to exist.",
         "needs_role_approval": "A person must confirm this test data is what it claims to be.",
         "ready_to_run": "Ready for the trial.",
         "outcome_recorded": "The verdict is ready. Opening it counts as one look."}
AUDIT_VERDICTS = {"CERTIFIED": "CERTIFIED", "NOT CONFIRMED": "NOT CERTIFIED", "INCONCLUSIVE": "INCONCLUSIVE",
                  "COULD NOT RUN": "COULD NOT RUN"}
WAITING = {
    "approval_node": ("Approve or reject a paused run", "A run has stopped and needs your decision."),
    "physical_protocol": ("Review a physical protocol", "Nothing is measured until a person approves the protocol."),
    "physical_data": ("Confirm returned measurements", "Measured data needs a person's confirmation."),
    "adapter": ("Approve a domain adapter", "A domain cannot be used until a person approves its checks."),
    "statement": ("Approve a formal statement", "A person must confirm the statement says what is meant."),
    "role": ("Confirm what a test dataset is", "The trial waits for a person to confirm where this data came from."),
    "alpha_account": ("Optional: give a test dataset more looks", "Without this, a dataset allows one claim."),
    "suite_review": ("Review a test suite", "An agent wrote it, so a person must read it."),
    "verifier_review": ("Review a generated checker", "An agent wrote it, so a person must read it."),
    "feature_ruling": ("Rule on a data feature", "A screen flagged a feature that may carry the answers."),
    "provider_signoff": ("Sign off a provider entry", "Agents cannot use the entry until a person signs it off."),
    "human_check": ("Register the check only a person holds", "One check must stay out of every agent's reach."),
    "human_check_run": ("Run the check only a person holds", "It has not passed on its current file."),
    "queue_error": ("A waiting list could not be read", "Something is broken: this list may be incomplete."),
    "needs_beacon": ("Give the public random value", STEPS["needs_beacon"]),
    "needs_data": ("Hand over the future data", STEPS["needs_data"]),
    "ready_to_run": ("Start the trial", STEPS["ready_to_run"]),
    "outcome_recorded": ("Open the verdict", STEPS["outcome_recorded"]),
    "health": ("Run the Court's health check", "New cases are refused until this version of the Court passes it."),
}


def jargon_problems(view: dict[str, Any], path: str = "") -> list[str]:
    """Internal terms in the sentences of a main view (as `model_dump`). Identifiers and quoted words are skipped."""
    out: list[str] = []
    for k, v in view.items():
        where = f"{path}.{k}"
        if k in NOT_SENTENCES:
            continue
        for item in v if isinstance(v, list) else [v]:
            if isinstance(item, dict):
                out += jargon_problems(item, where)
            elif isinstance(item, str):
                out += [f"{where}: {m.group(0)!r}" for m in _JARGON.finditer(item)]
    return out


PRIOR_NOTE = "A pinned model guessed this from the words alone. It can only make the test harder. Not calibrated."
NO_PRIOR = "No no-data estimate was measured: nothing checks whether this outcome was predictable from the words alone."
TRUST_LIMITS = (
    "No agent has run in a real Lab box yet.",
    "No discovery has been certified yet.",
    "The readiness numbers are rough and not calibrated.",
    "A name on this machine is a label, not a login: whoever holds this machine holds its records.",
    "The hardening planned for a public release is not done. This is a private tool.")


def lab_line(ev: dict[str, Any]) -> dict[str, str] | None:
    """One Story event as a line a person reads, or None for what is noise on a screen (file diffs, costs)."""
    p, kind = ev["payload"], ev["kind"]
    if kind == "idea":
        text = f"{p['op']}: {p.get('title') or p['idea_id']}" + (f" ({p['reason']})" if p.get("reason") else "")
    elif kind == "run":
        text = p.get("message") or p.get("command") or p.get("stage") or p.get("run_id", "")
        if p.get("metrics"):
            text += "  " + ", ".join(f"{m['name']} {m['value']:.4g} ({m['split']})" for m in p["metrics"])
    elif kind == "action":
        text = f"{p['tool']} {p.get('target', '')}".strip()
    elif kind == "human":
        text = p.get("note") or p["op"]
    elif kind == "web":
        text = f"{p.get('op')}: {p.get('host') or ''}".strip(": ")
    elif kind == "session":
        text = p["op"]
    else:
        return None
    who = {"human": "you", "agent": "agent"}.get(str(ev.get("actor", {}).get("type")), "system")
    at = time.strftime("%H:%M:%S", time.localtime(float(ev["at"])))
    return {"at": at, "who": who, "kind": kind, "text": str(text)}


def scope_sentence(claim: dict[str, Any], *, family: str | None, route: str,
                   window: dict[str, str] | None = None) -> str:
    """One generated sentence: what the claim says and where it holds (UPGRADES N16). Numbers in it are the pre-fixed
    claim's own. `window`: the time the data covers, a required part of the scope on real data."""
    effect = f"on {claim['metric']} by at least {claim['min_effect']}"
    span = f", for data from {window['start']} to {window['end']}" if window else ""
    if route == "future":
        return f"On data that did not exist when the claim was fixed{span}, the method beats the baseline {effect}."
    if route == "sealed":
        return f"On your sealed data{span}, the method beats the baseline {effect}."
    if route == "retrospective":
        return f"On public data, the method beats the baseline {effect}."
    return f"On data generated by the {family} family, the method beats the baseline {effect}; generator-scoped."


def badge_text(form: str) -> dict[str, str]:
    """A held badge from the Court's display form, e.g. `REPLICATED (a -> b, same family)`."""
    name = form.split(" ")[0]
    meaning = BADGES[name]
    if name == "REPLICATED":
        meaning += " " + REPLICATED_RISK
        if "same family" in form:
            meaning += " " + SAME_FAMILY
        elif "family unknown" in form:
            meaning += " " + UNKNOWN_FAMILY
    return {"name": name, "form": form, "meaning": meaning}


def verdict_of(label: str | None, state: str, reason_class: str | None = None) -> str | None:
    """The researcher's verdict word, from the Kernel's label as the display returned it. Only a label the Kernel issued
    as certified can read CERTIFIED. INCONCLUSIVE is only ever a failed claim: it can never read as a pass."""
    if state == "COULD NOT RUN":
        return "COULD NOT RUN"
    if label is None:
        return None
    if label.startswith("CERTIFIED"):
        return "CERTIFIED"
    return "INCONCLUSIVE" if reason_class == TOO_CLOSE else "NOT CERTIFIED"


def verdict_sentence(verdict: str | None, held: list[str], scope: str, reason_class: str | None, *,
                     retrospective: bool) -> str:
    if verdict is None:
        return "No verdict yet."
    if verdict != "CERTIFIED":
        return " ".join(s for s in (VERDICTS[verdict], REASONS.get(str(reason_class))) if s)
    head = f"{VERDICTS[verdict]} {scope}"
    if retrospective:
        return f"{head} {RETROSPECTIVE}"
    return head if "VALIDATED" in held else f"{head} {NOT_A_FINDING}"
