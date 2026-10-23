"""`chh lab ...` (GOAL §17). JSON on stdout. Every Lab output is EXPLORATORY and never a finding.

  chh lab agents                                  adapters, and why Cursor / Antigravity are deferred
  chh lab check                                   static wall check (lab root, image recipe, truth layout)
  chh lab start --agent NAME [credential, model and box options]
  chh lab attach SESSION [--prompt TEXT | --prompt-file FILE]     interactive UI, or one headless prompt
  chh lab status [SESSION]
  chh lab stop SESSION                            the box goes; the workspace stays
  chh lab promote SESSION --request FILE          freeze code + environment for the Court
  chh lab handoff PROMOTION --targets FILE        name the held-out targets; writes the Court spec (chh-audit/1)
  chh lab release-verdict PROMOTION --by NAME     HUMAN: the verdict label only, as a ledgered release

Built, NOT test-verified.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from ..store import EvidenceStore


def add_parsers(sub: Any) -> None:
    lab = sub.add_parser("lab", help="the Lab (GOAL §17): full-capability agent boxes; outputs are EXPLORATORY")
    s = lab.add_subparsers(dest="lab_cmd", required=True)
    s.add_parser("agents", help="list agent adapters (supported or deferred, with the reason)")
    s.add_parser("check", help="static wall check: lab root, docker/lab.Dockerfile, truth layout")
    st = s.add_parser("start", help="start a lab box with an agent inside (never on this host)")
    st.add_argument("--agent", required=True, help="claude-code | codex | opencode | acp")
    st.add_argument("--session", help="session id (default: generated)")
    st.add_argument("--registry-key", help="provider registry entry of the credential (e.g. anthropic-api)")
    st.add_argument("--credential-env", help="host variable CHH_LAB_CRED_<NAME> holding the scoped key")
    st.add_argument("--credential-file", help="file holding the scoped key (outside agent-readable roots)")
    st.add_argument("--spend-cap-usd", type=float, help="the spend cap the provider enforces on this key")
    st.add_argument("--scope", default="", help="where the key lives and what limits it (recorded)")
    st.add_argument("--ttl-hours", type=float, default=24.0, help="credential lifetime for this session")
    st.add_argument("--model")
    st.add_argument("--permission-mode", help="the agent's own mode inside the box (default: full access)")
    st.add_argument("--local-base-url", help="OpenCode + local model: OpenAI-compatible endpoint seen from the box")
    st.add_argument("--acp-preset", help="ACP agent preset (see `chh lab agents`)")
    st.add_argument("--acp-command", help="ACP agent stdio command inside the box (quoted)")
    st.add_argument("--box-env", help="ACP: the in-box variable the agent reads its key from")
    st.add_argument("--image", help="lab image (default custom-harness-lab:py312, built by the owner)")
    st.add_argument("--gpus", help="e.g. all (NVIDIA container toolkit on the host)")
    st.add_argument("--memory", default="16g")
    st.add_argument("--cpus", type=float)
    st.add_argument("--root", action="store_true", help="OPT-IN: root in the box with Docker's default capabilities "
                                                         "(system package installs); recorded")
    at = s.add_parser("attach", help="open the agent's interactive UI in the box, or run one headless prompt")
    at.add_argument("session")
    g = at.add_mutually_exclusive_group()
    g.add_argument("--prompt")
    g.add_argument("--prompt-file")
    ss = s.add_parser("status", help="sessions, their last event and box state")
    ss.add_argument("session", nargs="?")
    sp = s.add_parser("stop", help="stop and remove the box; the workspace is kept")
    sp.add_argument("session")
    pr = s.add_parser("promote", help="freeze a Lab idea (code + pinned environment) for the Court")
    pr.add_argument("session")
    pr.add_argument("--request", required=True, help="chh-lab-promote/1 YAML written by the researcher")
    ho = s.add_parser("handoff", help="name the held-out targets (after the freeze) and write the Court spec")
    ho.add_argument("promotion")
    ho.add_argument("--targets", required=True, help="chh-lab-targets/1 YAML")
    rv = s.add_parser("release-verdict", help="HUMAN: write the Court's verdict label into the session workspace")
    rv.add_argument("promotion")
    rv.add_argument("--by", required=True, help="the human releasing it (recorded)")


def _out(obj: Any) -> int:
    json.dump(obj, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


def cmd_lab(a: argparse.Namespace, store: EvidenceStore) -> int:
    from . import promote as P
    from . import session as S
    from .agents import ADAPTERS, PRESETS, AgentOptions
    from .box import LAB_IMAGE, BoxSpec
    from .credentials import CredentialSpec
    from .walls import static_problems

    c = a.lab_cmd
    if c == "agents":
        return _out({"agents": {n: {"supported": ad.supported, "reason": ad.unsupported_reason or None,
                                    "registry_entries": sorted(ad.credentials)} for n, ad in ADAPTERS.items()},
                     "acp_presets": {k: {"name": p.name, "acp": list(p.acp), "setup": p.setup_url}
                                     for k, p in PRESETS.items()}})
    if c == "check":
        problems = static_problems()
        _out({"problems": problems})
        return 1 if problems else 0
    if c == "start":
        opts = AgentOptions(registry_key=a.registry_key, model=a.model, permission_mode=a.permission_mode,
                            local_base_url=a.local_base_url, acp_preset=a.acp_preset,
                            acp_command=tuple(shlex.split(a.acp_command)) if a.acp_command else (),
                            box_env=a.box_env)
        cred = None
        if a.credential_env or a.credential_file:
            if a.spend_cap_usd is None:
                raise SystemExit("--spend-cap-usd is required with a credential")
            cred = CredentialSpec(registry_key=str(a.registry_key), box_env=a.box_env or "UNSET",
                                  spend_cap_usd=a.spend_cap_usd, scope=a.scope, source_env=a.credential_env,
                                  source_file=a.credential_file, ttl_s=a.ttl_hours * 3600)
        box = BoxSpec(image=a.image or LAB_IMAGE, gpus=a.gpus, memory=a.memory, cpus=a.cpus, root=a.root)
        return _out(S.start(store, a.agent, opts, cred, box=box, session_id=a.session))
    if c == "attach":
        if a.prompt is None and a.prompt_file is None:
            return S.attach(store, a.session)
        text = a.prompt if a.prompt is not None else Path(a.prompt_file).read_text(encoding="utf-8")
        return _out(S.run_prompt(store, a.session, text))
    if c == "status":
        return _out(S.status(store, a.session))
    if c == "stop":
        return _out(S.stop(store, a.session))
    if c == "promote":
        return _out(P.freeze(store, a.session, Path(a.request)))
    if c == "handoff":
        return _out(P.handoff(store, a.promotion, Path(a.targets)))
    if c == "release-verdict":
        return _out(P.release_verdict(store, a.promotion, a.by))
    raise SystemExit(f"unknown lab command {c!r}")


COMMANDS = {"lab": cmd_lab}
