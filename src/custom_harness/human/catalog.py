"""The catalogue of HUMAN-only actions (GOAL §4.7, §5 L3/L9, §10 invariants 13, 15, 22, §16.7).

One row per action a model, a module or an evolved harness node must never take. Each row names the command that
performs it, the exact hashed item the action binds (invariant 15: an approval binds only the item it names), the
ledger kind (and node id) it writes, and the one code path allowed to write it. docs/human_actions.md is the
readable form of this table; tests/test_human_static.py checks both against the code.

Import-light on purpose: studio/kill.py (the module Kernel's ledger) reads MODULE_FORBIDDEN_KINDS from here, so this
file imports nothing from the rest of the package.
"""

from __future__ import annotations

from dataclasses import dataclass

# Kernel ledger kinds that module code (the Kernel a module gets from ModuleContext.kernel()) may never write.
# studio/kill.py _ModuleLedger refuses them. Not listed, on purpose: "approval_request" (the engine records each
# pause), "alpha_reserve" (pre-registration), "alpha_retire" (the Kernel retires an exhausted account; the HUMAN
# early retirement, body code "human", is refused by _ModuleLedger separately), "release", "mint", "prereg" and
# the other Kernel-internal kinds.
MODULE_FORBIDDEN_KINDS: frozenset[str] = frozenset({
    "approval",  # approval nodes, finalize, audit gates, non-isolated compute, provider sign-off, human checks
    "adapter_approval",
    "role_approval",
    "statement_approval",
    "alpha_open",
    "feature_ruling",
    "verifier_review",
    "run_kill",
    "tier_policy",
    "salt_reveal",
})

# node ids of the "approval" kind used by human actions that have no run (run ids carry a prefix no engine run
# carries, so the engine never reads them as a run's approval; same scheme as audit/gate.py and evolve/finalize.py)
PROVIDER_SIGNOFF_NODE = "provider_signoff"
HUMAN_CHECK_NODE = "human_check_registration"


@dataclass(frozen=True)
class HumanAction:
    name: str  # stable id (the API / a future UI uses it)
    command: str  # the CLI command that performs it
    api: str  # the Python function a UI calls ("" = CLI only today)
    binds: str  # the exact hashed item the action names
    ledger_kind: str  # Kernel ledger kind written ("" = not the Kernel ledger; see `where`)
    node_id: str  # for kind "approval": the node id ("" otherwise)
    writer: str  # the one module allowed to append it
    queued: bool  # listed by `chh queue` when pending
    where: str = ""  # anything not in the Kernel ledger


ACTIONS: tuple[HumanAction, ...] = (
    HumanAction("approval_node", "chh approve RUN NODE --decision approve|reject --approver NAME --item-hash H",
                "human.nodes.approve_node", "sha256 of the canonical JSON of the paused item (run, node, item)",
                "approval", "<the approval node id>", "custom_harness.human.nodes", True),
    HumanAction("physical_protocol", "chh approve RUN protocol_review --decision ... --item-hash H",
                "human.nodes.approve_node", "the protocol + claim item hash (GOAL §4.7)", "approval",
                "protocol_review", "custom_harness.human.nodes", True),
    HumanAction("physical_data", "chh return-data RUN --file F --attest TEXT, then chh approve RUN data_received",
                "", "the returned dataset of exactly the approved protocol", "approval", "data_received",
                "custom_harness.human.nodes", True),
    HumanAction("adapter", "chh approve-adapter NAME --approver NAME [--adapter-hash H]",
                "human.actions.approve_adapter", "adapter content hash (kernel/adapters.adapter_hash)",
                "adapter_approval", "", "custom_harness.kernel.adapters", True),
    HumanAction("statement", "chh approve-statement FILE --artifact-id ID --approver NAME --statement-hash H",
                "human.nodes.approve_statement", "sha256 of the chh-lean-statement/1 file", "statement_approval", "",
                "custom_harness.human.nodes", True),
    HumanAction("role", "chh approve-role DATASET --approver NAME [--dataset-hash H]",
                "human.actions.approve_role", "dataset content hash", "role_approval", "",
                "custom_harness.kernel.vault", True),
    HumanAction("alpha_account", "chh alpha open DATASET --alpha A --slots S --approver NAME [--dataset-hash H]",
                "human.actions.open_alpha_account", "held-out target dataset hash", "alpha_open", "",
                "custom_harness.kernel.kernel", True),
    HumanAction("verifier_review", "chh review verifier REF --decision approve|reject --reviewer NAME "
                "--verifier-hash H", "human.review.review_verifier",
                "verifier spec hash AND the hash of the fault suite it was validated against", "verifier_review", "",
                "custom_harness.verify.synthesis", True),
    HumanAction("suite_review", "chh review suite PROBLEM --decision approve|reject --reviewer NAME --suite-hash H",
                "human.review.review_suite", "fault-suite hash (all four halves)", "verifier_review", "",
                "custom_harness.verify.synthesis", True),
    HumanAction("feature_ruling", "chh rule-feature TASK_DIR --feature F --ruling legitimate|leak --approver NAME "
                "--report-hash H --anchor FILE", "human.features.rule_feature",
                "data digest, tags hash and screen report hash", "feature_ruling", "",
                "custom_harness.verify.provenance", True),
    HumanAction("provider_signoff", "chh sign-off-provider KEY --by NAME --entry-hash H",
                "human.providers.sign_off_provider",
                "sha256 of the registry entry without verified_by/verified_on", "approval", PROVIDER_SIGNOFF_NODE,
                "custom_harness.human.providers", True),
    HumanAction("human_check", "chh register-human-check ID --by NAME --sha256 H", "human.checks.register_check",
                "sha256 of the check file's raw bytes (content never read as text)", "approval", HUMAN_CHECK_NODE,
                "custom_harness.human.checks", True,
                where="also <data_dir>/human/checks/registry.json and redteam/human_checks.lock"),
    HumanAction("human_check_run", "python scripts/human_check.py --run", "",
                "sha256 of each registered check file", "", "", "custom_harness.assure.humancheck", True,
                where="redteam/human_check_runs.jsonl (hash chain)"),
    HumanAction("kill", "chh kill RUN --by NAME", "human.actions.kill_run", "the run id", "run_kill", "",
                "custom_harness.studio.kill", False),
    HumanAction("tier_policy", "chh tier-policy pin --by NAME --sha256 H", "", "sha256 of tier_policy.json",
                "tier_policy", "", "custom_harness.studio.tierpolicy", False),
    HumanAction("alpha_retire", "(no CLI yet) Kernel.retire_alpha_account", "", "the target's account",
                "alpha_retire", "", "custom_harness.kernel.kernel", False),
    HumanAction("finalize", "chh finalize SPEC --suite S --approver NAME", "",
                "spec hash, baseline hash, sealed tasks, seeds", "approval", "finalize",
                "custom_harness.evolve.finalize", False),
    HumanAction("audit_fidelity", "chh audit approve-fidelity ...", "", "audit spec hash + claim hash",
                "approval", "audit_fidelity", "custom_harness.audit.gate", False),
    HumanAction("audit_reply", "chh audit reply ...", "", "report hash", "approval", "audit_reply",
                "custom_harness.audit.gate", False),
    HumanAction("audit_publication", "chh audit approve-publication ...", "", "report hash", "approval",
                "audit_publication", "custom_harness.audit.gate", False),
    HumanAction("non_isolated_compute", "(host code) compute.backend.record_non_isolated_approval", "",
                "the exact run request on that backend", "approval", "compute:non-isolated",
                "custom_harness.compute.backend", False),
    HumanAction("salt_reveal", "chh salts-reveal SEALED --by NAME", "", "campaign hash + commitment", "salt_reveal",
                "", "custom_harness.salts.plan", False),
    HumanAction("memory_revoke", "chh memory revoke ENTRY --reason R --by NAME", "", "the memory entry id", "", "",
                "custom_harness.memory.store", False, where="memory log (records table)"),
    HumanAction("feature_rulings_migrate", "chh feature-rulings-migrate --by NAME", "",
                "every line of the interim rulings.jsonl (chain verified)", "feature_ruling", "",
                "custom_harness.verify.provenance", False),
)

ACTION_NAMES: frozenset[str] = frozenset(a.name for a in ACTIONS)


def action(name: str) -> HumanAction:
    for a in ACTIONS:
        if a.name == name:
            return a
    raise KeyError(name)
