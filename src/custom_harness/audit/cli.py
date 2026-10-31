"""`chh audit ...` (GOAL §16.10). JSON on stdout. The HUMAN steps name the human and the hash they reviewed.

  chh audit show SPEC                       what a human must review (spec hash, claim, sentence)
  chh audit approve-fidelity SPEC --approver NAME --spec-hash H        HUMAN
  chh audit prepare SPEC [--cache DIR] [--allow-download]              pre-register, then ingest the pinned data
  chh approve-role DATASET --approver NAME                             HUMAN (existing command), per dataset
  chh audit run SPEC --docker [--snapshot ID]                          both arms, certify, verdict
  chh audit report SPEC                                                the report (from the certificate)
  chh audit reply SPEC --recorded-by NAME --notified WHO --notified-on DATE (--reply-file F | --no-reply)   HUMAN
  chh audit approve-publication SPEC --approver NAME --report-hash H   HUMAN
  chh audit publish SPEC --out DIR                                     the only export path (gated)
  chh audit labeldep PIPELINE DATA --id-column C --label-column L --docker      label-dependence audit
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from ..config import data_dir
from ..store import EvidenceStore
from . import claim_audit as CA
from . import data as D
from . import gate as G
from .spec import LoadedSpec, SpecError, load_spec


def add_parsers(sub: Any) -> None:
    au = sub.add_parser("audit", help="Claim Audit of a published claim / label-dependence audit of published code")
    s = au.add_subparsers(dest="audit_cmd", required=True)
    sh = s.add_parser("show", help="print what a human must review before approving fidelity")
    sh.add_argument("spec")
    af = s.add_parser("approve-fidelity", help="HUMAN: the typed claim is faithful to the paper's sentence")
    af.add_argument("spec")
    af.add_argument("--approver", required=True)
    af.add_argument("--spec-hash", required=True, help="full spec hash printed by `chh audit show`")
    af.add_argument("--note", default="")
    pr = s.add_parser("prepare", help="pre-register the claim, then ingest the pinned fresh datasets")
    pr.add_argument("spec")
    pr.add_argument("--cache", help="OpenML cache directory (default $CHH_DATA_DIR/openml)")
    pr.add_argument("--allow-download", action="store_true",
                    help="OWNER DECISION: fetch missing pinned datasets from openml.org (network)")
    rn = s.add_parser("run", help="run both arms under one contract, certify, record the verdict")
    rn.add_argument("spec")
    rn.add_argument("--docker", action="store_true", help="candidate Docker sandbox (required for real code)")
    rn.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; trusted code only)")
    rn.add_argument("--snapshot", help="frozen literature snapshot id for the citation check (offline)")
    rp = s.add_parser("report", help="print the audit report (rendered from the certificate)")
    rp.add_argument("spec")
    ry = s.add_parser("reply", help="HUMAN: record the right-of-reply step for the current report")
    ry.add_argument("spec")
    ry.add_argument("--recorded-by", required=True)
    ry.add_argument("--notified", required=True, help="who was notified, and how")
    ry.add_argument("--notified-on", required=True, type=date.fromisoformat)
    g = ry.add_mutually_exclusive_group(required=True)
    g.add_argument("--reply-file", help="the authors' reply (text file)")
    g.add_argument("--no-reply", action="store_true", help="no reply came within the reply period")
    ap = s.add_parser("approve-publication", help="HUMAN: this exact report may leave the machine")
    ap.add_argument("spec")
    ap.add_argument("--approver", required=True)
    ap.add_argument("--report-hash", required=True)
    ap.add_argument("--note", default="")
    pb = s.add_parser("publish", help="write the publication package (refused without reply + approval)")
    pb.add_argument("spec")
    pb.add_argument("--out", required=True)
    ld = s.add_parser("labeldep", help="label-dependence audit of a published pipeline (flipped/constant worlds)")
    ld.add_argument("pipeline")
    ld.add_argument("data", help="the full dataset CSV (with labels)")
    ld.add_argument("--id-column", required=True)
    ld.add_argument("--label-column", required=True)
    ld.add_argument("--seed", type=int, default=0)
    ld.add_argument("--timeout-s", type=float, default=600.0)
    ld.add_argument("--docker", action="store_true")
    ld.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; trusted code only)")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=1, sort_keys=True, default=str))


def _load(path: str) -> LoadedSpec:
    try:
        return load_spec(Path(path))
    except SpecError as e:
        raise SystemExit(f"refused: {e}") from e


def _outcome(store: EvidenceStore, loaded: LoadedSpec) -> dict[str, Any]:
    out = CA.latest_outcome(store, loaded.audit_id)
    if out is None:
        raise SystemExit(f"audit {loaded.audit_id} has no recorded outcome yet (`chh audit run`)")
    return out


def _factory(a: argparse.Namespace, root: Path) -> CA.SandboxFactory:
    from ..compute import unique_workspaces  # every call a NEW directory: a retry never inherits an attempt's files
    from ..sandbox import LocalSandbox
    if a.docker:
        from ..sandbox.docker import DockerSandbox
        return unique_workspaces(DockerSandbox, root)  # the candidate image (no bundled datasets)
    if a.dev_sandbox:
        return unique_workspaces(lambda ws: LocalSandbox(ws, i_understand_no_isolation=True), root)
    raise SystemExit("pass --docker (or --dev-sandbox for trusted code only)")


def cmd_audit(a: argparse.Namespace, store: EvidenceStore) -> int:
    try:
        return _dispatch(a, store)
    except G.AuditRefused as e:
        _print({"refused": str(e)})
        return 1


def _dispatch(a: argparse.Namespace, store: EvidenceStore) -> int:
    cmd = a.audit_cmd
    if cmd == "labeldep":
        from . import labeldep
        res = labeldep.label_dependence_audit(
            Path(a.pipeline).read_bytes(), Path(a.data).read_bytes(), id_column=a.id_column,
            label_column=a.label_column, seed=a.seed, timeout_s=a.timeout_s,
            sandbox_factory=_factory(a, data_dir() / "sandboxes" / f"labeldep-{int(time.time())}"))
        _print(dataclasses.asdict(res))
        return 0 if res.verdict in (labeldep.Verdict.CLEAN, labeldep.Verdict.FLAGGED) else 1
    loaded = _load(a.spec)
    claim = loaded.claim()
    if cmd == "show":
        _print({"audit_id": loaded.audit_id, "spec_hash": loaded.spec_hash, "claim_hash": claim.claim_hash(),
                "paper": loaded.spec.paper.model_dump(mode="json"), "typed_claim": claim.model_dump(mode="json"),
                "next": f"chh audit approve-fidelity {a.spec} --approver <you> --spec-hash {loaded.spec_hash}"})
        return 0
    if cmd == "approve-fidelity":
        seq = G.approve_fidelity(store, audit_id=loaded.audit_id, spec_hash=loaded.spec_hash,
                                 claim_hash=claim.claim_hash(), sentence=loaded.spec.paper.sentence,
                                 approver=a.approver, reviewed_hash=a.spec_hash, note=a.note)
        _print({"approved_fidelity": loaded.audit_id, "approver": a.approver.strip(), "ledger_seq": seq})
        return 0
    if cmd == "prepare":
        cache = D.OpenMLCache(Path(a.cache) if a.cache else data_dir() / "openml")
        _print(CA.prepare(store, loaded, cache, D.http_fetcher() if a.allow_download else None))
        return 0
    if cmd == "run":
        resolver = None
        if a.snapshot:
            from ..literature.client import offline_client
            from ..literature.snapshot import SnapshotStore
            resolver = offline_client(SnapshotStore(data_dir() / "literature"), a.snapshot)
        out = CA.run(store, loaded, _factory(a, data_dir() / "sandboxes" / f"audit-{loaded.audit_id}"),
                     resolver=resolver)
        _print(out)
        return 0 if out["verdict"] != CA.Verdict.COULD_NOT_RUN.value else 1
    out = _outcome(store, loaded)
    report = CA.current_report(store, out)
    if cmd == "report":
        from ..court.display import record_report_view
        record_report_view(store, loaded.audit_id, [d.dataset_id for d in loaded.spec.datasets])
        print(report)
        return 0
    if cmd == "reply":
        text = Path(a.reply_file).read_text(encoding="utf-8") if a.reply_file else None
        seq = G.record_reply(store, audit_id=loaded.audit_id, report_hash=out["report_hash"],
                             recorded_by=a.recorded_by, notified=a.notified, notified_on=a.notified_on,
                             reply_text=text)
        _print({"right_of_reply_recorded": loaded.audit_id, "report_hash": out["report_hash"], "ledger_seq": seq})
        return 0
    if cmd == "approve-publication":
        seq = G.approve_publication(store, audit_id=loaded.audit_id, report_hash=out["report_hash"],
                                    approver=a.approver, reviewed_hash=a.report_hash, note=a.note)
        _print({"approved_publication": loaded.audit_id, "approver": a.approver.strip(), "ledger_seq": seq})
        return 0
    if cmd == "publish":
        bpath = None
        if out.get("cert_hash"):
            bpath = CA.bundle(store, out, data_dir() / "bundles" / f"audit-{loaded.audit_id}.zip")
        _print(G.publish(store, out, report, Path(a.out), bundle_path=bpath,
                         redistribution=loaded.spec.redistribution.model_dump()))
        return 0
    raise SystemExit(f"unknown audit command {cmd!r}")


COMMANDS = {"audit": cmd_audit}
