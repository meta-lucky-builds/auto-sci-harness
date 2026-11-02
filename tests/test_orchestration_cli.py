"""CLI of the delegation driver: tokens travel in files (never argv), refusals exit 3 and are ledgered."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from custom_harness.orchestration.cli import main
from test_orchestration_driver import brief


def run(capsys, *argv: str) -> tuple[int, dict]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


def setup(tmp_path: Path, capsys) -> tuple[Path, Path, Path]:
    root, lead_tok = tmp_path / "orch", tmp_path / "lead.tok"
    code, _ = run(capsys, "register-lead", "--root", str(root), "--name", "lead-a", "--token-out", str(lead_tok))
    assert code == 0 and lead_tok.read_text().strip()
    bf = tmp_path / "brief.json"
    bf.write_text(json.dumps(brief()), encoding="utf-8")
    return root, lead_tok, bf


def test_cli_spawn_spend_complete(tmp_path: Path, capsys):
    root, lead_tok, bf = setup(tmp_path, capsys)
    htok = tmp_path / "helper.tok"
    code, out = run(capsys, "spawn", "--root", str(root), "--token-file", str(lead_tok), "--brief", str(bf),
                    "--token-out", str(htok))
    assert code == 0 and out["session"].startswith("helper-") and "token" not in out
    code, _ = run(capsys, "spend", "--root", str(root), "--token-file", str(htok), "--kind", "compute",
                  "--resource", "local:cpu", "--unit", "cpu_seconds", "--amount", "10")
    assert code == 0
    code, out = run(capsys, "complete", "--root", str(root), "--token-file", str(htok), "--output", "commit x")
    assert code == 0
    code, st = run(capsys, "status", "--root", str(root))
    assert code == 0 and st["in_flight"] == [] and st["sessions"][out["session"]]["finished"]


def test_cli_attack_nested_spawn_exits_3_and_is_ledgered(tmp_path: Path, capsys):
    root, lead_tok, bf = setup(tmp_path, capsys)
    htok = tmp_path / "helper.tok"
    run(capsys, "spawn", "--root", str(root), "--token-file", str(lead_tok), "--brief", str(bf), "--token-out",
        str(htok))
    b2 = tmp_path / "b2.json"
    b2.write_text(json.dumps(brief(brief_id="n", branch="h/n", worktree="wt/n")), encoding="utf-8")
    code, out = run(capsys, "spawn", "--root", str(root), "--token-file", str(htok), "--brief", str(b2),
                    "--token-out", str(tmp_path / "n.tok"))
    assert code == 3 and out == {"refused": "spawn", "reason": "nested_spawn",
                                 "detail": {"brief_id": "n", "parent": "lead-a"}}
    assert not (tmp_path / "n.tok").exists()
    code, st = run(capsys, "status", "--root", str(root))
    assert st["refusals"] == 1 and len(st["in_flight"]) == 1


def test_cli_attack_over_budget_exits_3(tmp_path: Path, capsys):
    root, lead_tok, bf = setup(tmp_path, capsys)
    htok = tmp_path / "helper.tok"
    run(capsys, "spawn", "--root", str(root), "--token-file", str(lead_tok), "--brief", str(bf), "--token-out",
        str(htok))
    code, out = run(capsys, "spend", "--root", str(root), "--token-file", str(htok), "--kind", "model_quota",
                    "--resource", "model:fake-family-a", "--unit", "calls", "--amount", "6")
    assert code == 3 and out["reason"] == "spend_over_budget"


def test_cli_verify_detects_tampering(tmp_path: Path, capsys):
    root, _, _ = setup(tmp_path, capsys)
    assert run(capsys, "verify", "--root", str(root))[0] == 0
    p = root / "ledger.jsonl"
    p.write_text(p.read_text(encoding="utf-8").replace("lead-a", "lead-z"), encoding="utf-8", newline="")
    code, out = run(capsys, "verify", "--root", str(root))
    assert code == 2 and "hash chain" in out["error"]


def test_cli_refuses_token_on_command_line(tmp_path: Path, capsys):
    root, lead_tok, bf = setup(tmp_path, capsys)
    try:
        main(["spawn", "--root", str(root), "--token", lead_tok.read_text(), "--brief", str(bf)])
    except SystemExit as e:
        assert e.code == 2  # argparse: no such option; tokens never go in argv
    else:
        raise AssertionError("a --token option must not exist")


def test_module_entry_point(tmp_path: Path):
    r = subprocess.run([sys.executable, "-m", "custom_harness.orchestration", "status", "--root",
                        str(tmp_path / "o")], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["in_flight"] == []


def test_chh_delegate_runs_the_delegation_driver(tmp_path: Path, capsys):
    """`chh delegate <command>` is the same driver CLI, reachable from the main `chh` entry point."""
    from custom_harness.cli import main as chh

    root, lead_tok = tmp_path / "orch", tmp_path / "lead.tok"
    assert chh(["delegate", "register-lead", "--root", str(root), "--name", "lead-a",
                "--token-out", str(lead_tok)]) == 0
    capsys.readouterr()
    assert chh(["delegate", "status", "--root", str(root)]) == 0
    st = json.loads(capsys.readouterr().out)
    assert "lead-a" in st["sessions"] and st["in_flight"] == []
