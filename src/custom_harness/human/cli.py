"""`chh` commands of the human-review surface (CLI over `custom_harness.human`; GOAL §5 L3, L9, invariants 13/15/22).

New HUMAN commands (each needs an interactive terminal and a typed confirmation of the hash it binds):
  chh review verifier REF --decision approve|reject --reviewer NAME --verifier-hash H [--note T]
  chh review suite PROBLEM --decision approve|reject --reviewer NAME --suite-hash H [--note T]
  chh sign-off-provider KEY --by NAME --entry-hash H [--note T] [--override FILE]
  chh register-human-check ID --by NAME --sha256 H [--file NAME] [--repo ROOT] [--replace]
Read-only:
  chh review verifier REF --show | chh review suite PROBLEM --show (terminal only: it shows the sealed half)
  chh review list | chh sign-off-provider KEY --show | chh human-actions [--json]
`chh queue` (studio/cli.py) lists every pending human action through `human.queue.pending`.

`check_legacy_actor` runs before every command in cli.main: the older human commands (`approve`, `approve-adapter`,
`approve-role`, `alpha open`, `approve-statement`, `finalize`, `kill`, `tier-policy pin`, `salts-reveal`, `memory
revoke`, `feature-rulings-migrate`, `audit ...`) get the same agent-name refusal, and `approve-adapter`, `approve-role`
and `alpha open` get an optional hash binding (`--adapter-hash`, `--dataset-hash`) checked before they run. An empty
name is left to each command's own refusal (its message is part of its tests).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from .guard import HumanActionRefused, check_human_name, require_tty

CONFIRM_CHARS = 12


def _refused(e: Exception) -> SystemExit:
    return SystemExit(f"refused: {e}")


def _confirm(full_hash: str, what: str) -> None:
    """The human re-types the first CONFIRM_CHARS characters of the hash they are binding."""
    try:
        typed = input(f"Type the first {CONFIRM_CHARS} characters of the {what} to confirm: ")
    except EOFError:
        raise HumanActionRefused("no confirmation typed; nothing recorded") from None
    if typed.strip().lower() != full_hash[:CONFIRM_CHARS].lower():
        raise HumanActionRefused("confirmation did not match the hash; nothing recorded")


def _print(doc: Any) -> None:
    print(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True, default=str))


def _decision(a: argparse.Namespace) -> bool:
    if a.decision not in ("approve", "reject"):
        raise HumanActionRefused("pass --decision approve|reject (or --show to look only)")
    return bool(a.decision == "approve")


# ------------------------------------------------------------------ chh review
def cmd_review(a: argparse.Namespace, store: EvidenceStore) -> int:
    from . import review as R
    try:
        if a.review_cmd == "list":
            _print(R.pending_suites(store) + R.pending_verifiers(store))
            return 0
        if a.review_cmd == "verifier":
            view = R.verifier_view(store, a.ref)
            if a.show:
                _print(view)
                return 0
            require_tty("chh review verifier")
            approve = _decision(a)
            _print(view)
            if not a.verifier_hash:
                raise HumanActionRefused("pass --verifier-hash (the hash shown above, after you reviewed it)")
            _confirm(a.verifier_hash, "verifier hash")
            e = R.review_verifier(store, a.ref, reviewer=a.reviewer or "", approve=approve,
                                  verifier_hash=a.verifier_hash, note=a.note)
        else:
            if a.show:
                require_tty("chh review suite --show")  # the sealed half is shown: a terminal only
                _print(R.suite_view(a.problem, store))
                return 0
            require_tty("chh review suite")
            approve = _decision(a)
            _print(R.suite_view(a.problem, store))
            if not a.suite_hash:
                raise HumanActionRefused("pass --suite-hash (the hash shown above, after you reviewed it)")
            _confirm(a.suite_hash, "suite hash")
            e = R.review_suite(store, a.problem, reviewer=a.reviewer or "", approve=approve, suite_hash=a.suite_hash,
                               note=a.note)
    except HumanActionRefused as err:
        raise _refused(err) from None
    print(json.dumps({"recorded": "approved" if e.body.get("approve") else "rejected", "kind": e.kind,
                      "reviewer": e.body.get("reviewer"), "ledger_seq": e.seq}))
    return 0


# ------------------------------------------------------------------ chh sign-off-provider
def cmd_sign_off_provider(a: argparse.Namespace, store: EvidenceStore) -> int:
    from . import providers as P
    override = Path(a.override) if a.override else None
    try:
        view = P.provider_view(a.key, store, override)
        if a.show:
            _print(view)
            return 0
        require_tty("chh sign-off-provider")
        _print(view)
        if not a.entry_hash:
            raise HumanActionRefused("pass --entry-hash (the hash shown above, after you checked the entry)")
        _confirm(a.entry_hash, "entry hash")
        e = P.sign_off_provider(store, a.key, by=a.by or "", entry_hash_reviewed=a.entry_hash, note=a.note,
                                override=override)
    except HumanActionRefused as err:
        raise _refused(err) from None
    print(json.dumps({"signed_off": a.key, "by": e.body["approver"], "entry_hash": e.body["provider"]["entry_hash"],
                      "verified_on": e.body["provider"]["verified_on"], "ledger_seq": e.seq}))
    return 0


# ------------------------------------------------------------------ chh register-human-check
def cmd_register_human_check(a: argparse.Namespace, store: EvidenceStore) -> int:
    from . import checks as K
    try:
        require_tty("chh register-human-check")
        if not a.sha256:
            raise HumanActionRefused("pass --sha256 (of the check file you wrote)")
        print(json.dumps({"id": a.id, "file": a.file or f"{a.id}.py", "sha256": a.sha256,
                          "note": "the file content is hashed only: never printed, decoded or recorded"}))
        _confirm(a.sha256, "sha256")
        e = K.register_check(store, a.id, by=a.by or "", sha256=a.sha256, file_name=a.file,
                             repo_root=Path(a.repo) if a.repo else None, replace=a.replace)
    except HumanActionRefused as err:
        raise _refused(err) from None
    print(json.dumps({"registered": a.id, "sha256": e.body["check"]["sha256"], "by": e.body["approver"],
                      "ledger_seq": e.seq, "next": "commit redteam/human_checks.lock; run python "
                                                  "scripts/human_check.py --run once per milestone"}))
    return 0


# ------------------------------------------------------------------ chh human-actions
def cmd_human_actions(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .catalog import ACTIONS
    rows = [asdict(x) for x in ACTIONS]
    if a.json:
        _print(rows)
    else:
        for r in rows:
            kind = r["ledger_kind"] + (f"/{r['node_id']}" if r["node_id"] else "") if r["ledger_kind"] else r["where"]
            print(f"{r['name']}: {r['command']}\n    binds: {r['binds']}\n    ledger: {kind}"
                  f"{'  (queued)' if r['queued'] else ''}")
    return 0


# ------------------------------------------------------------------ legacy commands: actor check + hash binding
_LEGACY_ACTOR_ATTRS: dict[str, tuple[str, ...]] = {
    "approve": ("approver",), "approve-adapter": ("approver",), "approve-role": ("approver",),
    "alpha": ("approver",), "approve-statement": ("approver",), "finalize": ("approver",), "kill": ("by",),
    "tier-policy": ("by",), "salts-reveal": ("by",), "memory": ("by",), "feature-rulings-migrate": ("by",),
    "rule-feature": ("approver",), "audit": ("approver", "recorded_by"),
}


def check_legacy_actor(a: argparse.Namespace, store: EvidenceStore) -> None:
    """Agent-name refusal for the older human commands, and the optional hash bindings (SystemExit on refusal)."""
    try:
        for attr in _LEGACY_ACTOR_ATTRS.get(str(getattr(a, "cmd", "")), ()):
            name = getattr(a, attr, None)
            if isinstance(name, str) and name.strip():
                check_human_name(name, what=f"--{attr.replace('_', '-')}")
        from .actions import adapter_hash_now, dataset_hash_now
        if a.cmd == "approve-adapter" and getattr(a, "adapter_hash", None):
            now = adapter_hash_now(a.name)
            if a.adapter_hash != now:
                raise HumanActionRefused(f"adapter {a.name} hashes to {now}, not the {a.adapter_hash} you reviewed")
        expect = getattr(a, "dataset_hash", None)
        if expect and (a.cmd == "approve-role" or (a.cmd == "alpha" and getattr(a, "alpha_cmd", "") == "open")):
            now = dataset_hash_now(store, a.dataset_id)
            if expect != now:
                raise HumanActionRefused(f"dataset {a.dataset_id} hashes to {now}, not the {expect} you reviewed")
    except HumanActionRefused as e:
        raise _refused(e) from None


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser | None:
    for act in parser._actions:  # argparse keeps sub-commands in a _SubParsersAction (no public accessor)
        if isinstance(act, argparse._SubParsersAction) and name in act.choices:
            sp = act.choices[name]
            return sp if isinstance(sp, argparse.ArgumentParser) else None
    return None


def add_parsers(sub: Any) -> None:
    """Add the new commands, and the optional hash flags of the older ones (call after they exist)."""
    rv = sub.add_parser("review", help="HUMAN: review synthesized verifiers and their fault suites (L3)")
    rsub = rv.add_subparsers(dest="review_cmd", required=True)
    rsub.add_parser("list", help="verifiers and suites waiting for a human review (read-only)")
    v = rsub.add_parser("verifier", help="show / approve / reject one synthesized verifier (by blob ref)")
    v.add_argument("ref", help="blob ref of the stored verifier (from chh queue or chh review list)")
    v.add_argument("--show", action="store_true", help="only show it (sealed half by counts only)")
    v.add_argument("--decision", choices=["approve", "reject"])
    v.add_argument("--reviewer", help="the human reviewing (recorded in the ledger)")
    v.add_argument("--verifier-hash", help="full hash of the verifier you reviewed; refused if it differs")
    v.add_argument("--note", default="")
    s = rsub.add_parser("suite", help="show / approve / reject one fault suite (both halves are shown)")
    s.add_argument("problem")
    s.add_argument("--show", action="store_true", help="only show it (terminal only)")
    s.add_argument("--decision", choices=["approve", "reject"])
    s.add_argument("--reviewer", help="the human reviewing (recorded in the ledger)")
    s.add_argument("--suite-hash", help="full hash of the suite you reviewed; refused if it differs")
    s.add_argument("--note", default="")
    p = sub.add_parser("sign-off-provider", help="HUMAN: sign off one provider registry entry (verify-registry "
                                                 "--release counts ledgered sign-offs only)")
    p.add_argument("key")
    p.add_argument("--show", action="store_true", help="only show the entry and its hash")
    p.add_argument("--by", help="the human signing off (recorded in the ledger)")
    p.add_argument("--entry-hash", help="full hash of the entry you checked; refused if it differs")
    p.add_argument("--note", default="")
    p.add_argument("--override", help="registry override file (default: $CHH_DATA_DIR/providers.override.yaml)")
    h = sub.add_parser("register-human-check", help="HUMAN: register a human-held check by the sha256 of its file "
                                                     "(invariant 22)")
    h.add_argument("id")
    h.add_argument("--by", help="the human registering it (recorded)")
    h.add_argument("--sha256", help="sha256 of the check file's bytes")
    h.add_argument("--file", help="file name inside <data_dir>/human/checks (default ID.py)")
    h.add_argument("--repo", help="repository root holding redteam/human_checks.lock (default: this checkout)")
    h.add_argument("--replace", action="store_true", help="replace a check already registered under this id")
    ha = sub.add_parser("human-actions", help="every human-only action: command, what it binds, ledger kind")
    ha.add_argument("--json", action="store_true")
    aa = sub.choices.get("approve-adapter")
    if aa is not None:
        aa.add_argument("--adapter-hash", default=None, help="adapter content hash you reviewed; refused if it differs")
    ar = sub.choices.get("approve-role")
    if ar is not None:
        ar.add_argument("--dataset-hash", default=None, help="dataset content hash you reviewed; refused if it differs")
    al = sub.choices.get("alpha")
    alo = _subparser(al, "open") if al is not None else None
    if alo is not None:
        alo.add_argument("--dataset-hash", default=None, help="dataset content hash you reviewed; refused if it "
                                                               "differs")


COMMANDS = {"review": cmd_review, "sign-off-provider": cmd_sign_off_provider,
            "register-human-check": cmd_register_human_check, "human-actions": cmd_human_actions}
