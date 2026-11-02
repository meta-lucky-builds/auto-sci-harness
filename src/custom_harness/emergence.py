"""Emergence rule, static half (GOAL §16.10): "Novelty may steer budget, never acceptance."

`check_novelty_isolation(src_root)` parses every .py file of the `custom_harness` package with `ast` (comments and
docstrings are not code and are ignored) and collects every identifier, attribute name, argument / keyword name,
import name and string constant containing "novel" or "interesting" (case-insensitive; covers novelty, novel,
interesting, interestingness). Every occurrence must be listed in ALLOWLIST as (file, token) -> (category, reason):

    "budget"       a novelty / interestingness signal that may steer allocation (which task, family or branch gets
                   the next evaluation). Allowed only in a BUDGET_PATHS module, never in an ACCEPTANCE_PATHS one.
    "not-a-score"  the word names something that is not a novelty score at all (a checklist row about literature
                   novelty; the label of the exact-duplicate refusal of the evolve loop).

Anything else is a problem, and so is an ACCEPTANCE_PATHS module importing a BUDGET_PATHS module (a budget signal
must not reach an accept / reject / certify / promote / admit decision by import either). The configuration itself is
checked too: a path pattern matching no file, a file in both path sets, an unknown category, a "budget" entry outside
BUDGET_PATHS, or an allowlist entry with no occurrence (stale) is a problem, so the lists cannot silently rot.

What this is NOT: a data-flow proof. A token scan catches the honest and the careless (a `novelty_score` added to
a gate), not an adversary who renames the value or builds the attribute name at run time. The dynamic half of the
rule (generated task families enter the evolve split only) is enforced in research/suite.py (`assign_splits`,
`check_split_isolation`, called by evolve/driver.load_suite).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

TOKEN = re.compile(r"\w*(?:novel|interesting)\w*", re.IGNORECASE)
CATEGORIES = ("budget", "not-a-score")

# Modules where an accept / reject / certify / promote / admit decision is made, relative to the package directory
# (`dir/**` = every file below dir). Why each one:
ACCEPTANCE_PATHS: tuple[str, ...] = (
    "kernel/**",                 # the Evidence Kernel: mints, refuses and retracts certificates (K0-K9, checklist)
    "verify/**",                 # trusted metrics, stats, leak audit, exclusion accounting the gates rely on
    "memory/**",                 # L5 store: the admit gate (ACTIVE / REJECTED), exploit quarantine, content screen
    "evolve/evaluate.py",        # the objective per run and the held-out family check that acceptance uses
    "evolve/selection.py",       # the deterministic acceptance gate, non-regression, cost bound
    "evolve/finalize.py",        # the one-shot sealed-split evaluation of a frozen harness
    "evolve/loop.py",            # one generation: static rejects, novelty (duplicate) gate, accept / reject_gate
    "evolve/multiplicity.py",    # campaign-level certification order and Bonferroni accounting
    "evolve/edits.py",           # static edit checks (EditRejected, protected surfaces)
    "evolve/driver.py",          # carries the accepted harness across generations; load_suite's split checks
    "research/claims.py",        # deterministic claim, pre-registration and certification of the research loop
    "research/tree.py",          # experiment-tree node lifecycle (a node's accepted / rejected state)
    "research/preflight.py",     # certifiability preflight (may only deny)
    "research/modules.py",       # research-loop modules, including dev scoring and best-candidate selection
    "research/physical.py",      # physical claim path up to the Kernel: screen, prereg, protocol approval
    "research/socratic.py",      # question-only critic filter: keeps or drops critic items
    "research/suite.py",         # split assignment: which tasks may decide acceptance (gate / sealed)
    "literature/cite.py",        # citation VERIFIED / not verified, read by K0 and audit I3
    "gateway/role_exam.py",      # admits a second model family to a judging role
    "tiers.py",                  # verifier tiers and the tier policy that gates autonomy
    "policy.py",                 # human-owned run policy: model-family separation for judging roles
    "evolve/branches.py",        # L6.9: records a branch only on released cluster bits and a passing LOFO/LODO bit
    "evolve/router.py",          # L6.9: serves only a LOFO-checked route table; the one-shot sealed finalize_routed
    "evolve/search.py",          # L7: carries each island's accepted harness across rounds (like driver.py)
    "control/islands.py",        # L7: set_policy adopts an orchestrator policy only on a passing trial decision
    "control/policy_gate.py",    # L7: accepts or refuses an orchestrator policy change (exact sign test)
)

# Modules that allocate budget by a novelty / interestingness signal. NOT budget.py: that is the hard-cap cost ledger
# (usd, tokens, ...), novelty-free, and imported by the acceptance modules evolve/loop.py and evolve/driver.py to stop
# a campaign that runs out of money.
BUDGET_PATHS: tuple[str, ...] = (
    # int18 (stage-a, found by this checker at the s4-station merge): the station archive's stall signal. A newly
    # admitted POSITIVE entry on a (metric, subject) not yet in the archive is "progress"; it feeds only the stall
    # rule (stagnation lanes, the budget-cap shrink through the downgrade gate). The publication decision (PubCode)
    # is made before it and never reads it.
    "research/station/archive.py",
)

# (file relative to the package, lower-cased token) -> (category, reason). Each reason was checked by reading the code.
ALLOWLIST: dict[tuple[str, str], tuple[str, str]] = {
    ("kernel/checklist.py", "novelty_verification"): (
        "not-a-score", "reporting-checklist row key: a free-text answer about literature novelty; checklist.missing "
                       "only tests that an answer is present (absence blocks publication, presence grants nothing)"),
    ("kernel/checklist.py", "novelty"): (
        "not-a-score", "the `novelty` text argument of checklist.fill and the row title 'Novelty verification "
                       "against the literature'; copied verbatim into the checklist, never compared or scored"),
    ("research/claims.py", "novelty"): (
        "not-a-score", "keyword argument passing the checklist text 'not performed: the literature module does not "
                       "exist yet' to kernel.checklist.fill"),
    ("evolve/loop.py", "rejected_novelty"): (
        "not-a-score", "lineage status of the duplicate gate: an edit whose normalised signature (sha256 of type + "
                       "sorted ops) EQUALS a previously rejected one is recorded and skipped (`continue`) before any "
                       "evaluation; equality of hashes, no score; it can only deny, never accept"),
    ("evolve/export.py", "rejected_novelty"): (
        "not-a-score", "name of the duplicate-gate counter in the proposer-visible generation_stats whitelist"),
    ("control/islands.py", "rejected_novelty"): (
        "not-a-score", "the duplicate gate's lineage status, matched only to collect the EDIT TYPES an island tried "
                       "(edit-type quota); no score: islands decide budget shares and adopt a policy change only on a "
                       "passing trial decision; no edit accept/reject "
                       "(reviewed at the g-router merge, 2026-10-08)"),
    ("ablation.py", "novelty"): (
        "not-a-score", "name of the `novelty-gate` ablation switch and the registry text describing it; a switch "
                       "name is referenced by tests and campaigns and cannot be renamed; registry text, not a scorer"),
    ("ablation.py", "rejected_novelty"): (
        "not-a-score", "the duplicate gate's lineage status quoted in the `novelty-gate` switch description; text "
                       "only, nothing is compared or scored"),
    ("ablation.py", "test_novelty_gate_switch"): (
        "not-a-score", "pytest node name registered as the `novelty-gate` switch's test (tests/"
                       "test_ablation_switches_evolve.py); a string looked up by name, not a scorer"),
    ("research/station/archive.py", "novel"): (
        "budget", "Publication.progress of a newly published POSITIVE entry: (metric, subject hash) not already "
                  "readable in the archive (set membership, no score); read only by the station's stall signal "
                  "(lanes, cap shrink), after the publication code is fixed; reviewed at the int18 merge"),
    ("audit/checklist.py", "novelty"): (
        "not-a-score", "keyword argument `novelty` of kernel.checklist.fill (name fixed there), passed the text "
                       "'not applicable: a Claim Audit ... makes no new claim'; copied verbatim, never scored"),
    # this checker names the tokens it looks for (its regex, this allowlist's keys, its messages); it reads source
    # files and decides nothing about any claim, edit, task or entry. A NEW token here is still flagged.
    **{("emergence.py", tok): ("not-a-score", "the checker's own pattern, allowlist keys and problem messages")
       for tok in ("check_novelty_isolation", "novel", "interesting", "novelty", "interestingness",
                   "novelty_verification", "rejected_novelty", "test_novelty_gate_switch")},
}


def _matches(rel: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return rel.startswith(pattern[:-2])
    return rel == pattern


def _in(rel: str, patterns: Iterable[str]) -> bool:
    return any(_matches(rel, p) for p in patterns)


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def _tokens(tree: ast.AST) -> list[tuple[str, int]]:
    """(lower-cased token, line) for every matching name or string constant, docstrings excluded."""
    skip = _docstring_ids(tree)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        line = getattr(node, "lineno", 0)
        for _field, value in ast.iter_fields(node):
            texts = [value] if isinstance(value, str | bytes) else \
                [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
            for text in texts:
                s = text.decode("latin-1") if isinstance(text, bytes) else text
                out += [(m.group(0).lower(), line) for m in TOKEN.finditer(s)]
    return out


def _imported(rel: str, tree: ast.AST) -> set[str]:
    """Package-relative file paths a module may import (both `x.py` and `x/__init__.py` candidates)."""
    pkg = rel.split("/")[:-1]
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[:len(pkg) - (node.level - 1)] if node.level - 1 <= len(pkg) else []
                prefix = ".".join(["custom_harness", *base, *([node.module] if node.module else [])])
            else:
                prefix = node.module or ""
            mods.add(prefix)
            mods |= {f"{prefix}.{a.name}" for a in node.names}
    out: set[str] = set()
    for m in mods:
        parts = m.split(".")
        if parts[0] != "custom_harness":
            continue
        sub = "/".join(parts[1:])
        out |= {f"{sub}.py", f"{sub}/__init__.py" if sub else "__init__.py"}
    return out


def check_novelty_isolation(src_root: Path, *, acceptance_paths: Iterable[str] = ACCEPTANCE_PATHS,
                            budget_paths: Iterable[str] = BUDGET_PATHS,
                            allowlist: Mapping[tuple[str, str], tuple[str, str]] = ALLOWLIST) -> list[str]:
    """Problems (empty = the rule holds as far as a token scan can tell). `src_root` is the `src` directory that
    contains the `custom_harness` package. Fails closed: no package, no file, or an unparsable file is a problem."""
    acceptance, budget = tuple(acceptance_paths), tuple(budget_paths)
    pkg = Path(src_root) / "custom_harness"
    files = sorted(pkg.rglob("*.py")) if pkg.is_dir() else []
    if not files:
        return [f"no custom_harness package with .py files under {src_root}"]
    rels = {f: f.relative_to(pkg).as_posix() for f in files}
    problems: list[str] = []
    for kind, patterns in (("ACCEPTANCE_PATHS", acceptance), ("BUDGET_PATHS", budget)):
        problems += [f"{kind} pattern {p!r} matches no file" for p in patterns
                     if not any(_matches(r, p) for r in rels.values())]
    problems += [f"{r} is in both ACCEPTANCE_PATHS and BUDGET_PATHS" for r in rels.values()
                 if _in(r, acceptance) and _in(r, budget)]
    for (rel, tok), (cat, _reason) in sorted(allowlist.items()):
        if cat not in CATEGORIES:
            problems.append(f"allowlist ({rel}, {tok}): unknown category {cat!r}")
        elif cat == "budget" and (_in(rel, acceptance) or not _in(rel, budget)):
            problems.append(f"allowlist ({rel}, {tok}): a 'budget' token is allowed only in a BUDGET_PATHS module "
                            f"outside ACCEPTANCE_PATHS")
    seen: set[tuple[str, str]] = set()
    for f, rel in rels.items():
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            problems.append(f"{rel}: cannot be parsed ({type(e).__name__}); not checked")
            continue
        for tok, line in _tokens(tree):
            seen.add((rel, tok))
            entry = allowlist.get((rel, tok))
            if entry is None:
                problems.append(f"{rel}:{line}: novelty/interestingness token {tok!r} is not allowlisted "
                                f"(GOAL §16.10: novelty may steer budget, never acceptance)")
            elif entry[0] == "budget" and _in(rel, acceptance):
                problems.append(f"{rel}:{line}: budget token {tok!r} inside an acceptance module")
        if _in(rel, acceptance):
            problems += [f"{rel}: acceptance module imports budget module {t}" for t in sorted(_imported(rel, tree))
                         if t in rels.values() and _in(t, budget)]
    problems += [f"allowlist ({rel}, {tok}) has no occurrence (stale entry)" for rel, tok in sorted(allowlist)
                 if (rel, tok) not in seen]
    return problems
