"""The provider registry verified each release (GOAL §5 L0 "Model gateway"; invariant 11: BYOS only for allowlisted
providers through official auth; training only on licence-permitted outputs). Offline and static.

`chh verify-registry` checks the EFFECTIVE registry (bundled providers.yaml + the local override file) against the
human-owned BYOS allowlist (gateway/registry.py) and the policy; `--release` adds the per-release human verification
and cost rows. The runtime gate `require_usable` enforces the same allowlist, so an override file cannot widen it.
"""

import datetime as dt
import json

import pytest

from custom_harness import cli
from custom_harness.gateway import ProviderBlocked, load_registry, require_usable
from custom_harness.gateway import registry as R

TODAY = dt.date(2026, 10, 8)
GOOD_API = {"provider": "openai", "mode": "api", "auth_kind": "api_key", "byos_status": "n/a",
            "training_on_outputs": "forbidden"}


def test_bundled_registry_passes_the_static_policy_but_not_the_release_check():
    reg = load_registry()
    assert R.policy_problems(reg, owner_training={}) == []
    assert R.verify_registry(reg, release=False, owner_training={}) == []
    rel = R.verify_registry(reg, release=True, today=TODAY, owner_training={})
    assert any(p.startswith("openai-codex-cli: not verified by a human") for p in rel)  # honest state today


@pytest.mark.parametrize("entry,needle", [
    ({**GOOD_API, "provider": "newco"}, "unknown provider 'newco'"),
    ({**GOOD_API, "provider": "mock"}, "unknown provider 'mock'"),  # test-only provider never in a registry
    ({k: v for k, v in GOOD_API.items() if k != "training_on_outputs"}, "missing licence flag"),
    ({**GOOD_API, "training_on_outputs": None}, "missing licence flag"),
    ({k: v for k, v in GOOD_API.items() if k != "auth_kind"}, "missing auth_kind"),
    ({**GOOD_API, "auth_kind": "session_cookie"}, "not an official auth kind"),
    ({"provider": "openai", "mode": "byos", "auth_kind": "reused_token", "byos_status": "allowed",
      "training_on_outputs": "forbidden", "source_url": "https://x", "auth_flow": "f"}, "not an official auth kind"),
])
def test_policy_flags_unknown_providers_missing_licence_flags_and_unofficial_auth(entry, needle):
    problems = R.policy_problems({"e": entry}, owner_training={})
    assert any(needle in p for p in problems), problems


def test_a_subscription_entry_off_the_allowlist_fails_even_when_marked_allowed():
    entry = {"provider": "anthropic", "mode": "byos", "auth_kind": "official_oauth", "byos_status": "allowed",
             "training_on_outputs": "forbidden", "source_url": "https://example.com/terms", "auth_flow": "oauth"}
    problems = R.policy_problems({"anthropic-subscription": entry}, owner_training={})
    assert any("not on the BYOS allowlist" in p for p in problems), problems
    codex = {**load_registry()["openai-codex-cli"]}
    assert R.policy_problems({"openai-codex-cli": codex}, owner_training={}) == []
    for bad, needle in (({**codex, "provider": "anthropic"}, "differs from the BYOS allowlist"),
                        ({**codex, "auth_kind": "official_oauth"}, "not the official kind"),
                        ({**codex, "source_url": "http://insecure"}, "https source_url"),
                        ({**codex, "mode": "api", "byos_status": "n/a", "auth_kind": "api_key"},
                         "must have mode 'byos'")):
        assert any(needle in p for p in R.policy_problems({"openai-codex-cli": bad}, owner_training={})), needle


def test_owner_training_flag_must_match_the_recorded_terms():
    reg = {"openai-api": GOOD_API}
    assert any("records the terms as ['forbidden']" in p
               for p in R.policy_problems(reg, owner_training={"openai/api": True}))
    assert any("no registry entry" in p for p in R.policy_problems(reg, owner_training={"local/ow": True}))
    assert R.policy_problems(reg, owner_training={"openai/api": False}) == []


def test_override_file_cannot_widen_the_byos_allowlist_at_runtime(tmp_path):
    """The runtime gate used to admit ANY byos entry whose status said 'allowed', so a local override file could switch
    on a prohibited subscription. It must also be on the code-owned allowlist with its official auth kind."""
    override = tmp_path / "o.yaml"
    override.write_text("anthropic-subscription:\n  provider: anthropic\n  mode: byos\n  auth_kind: official_oauth\n"
                        "  byos_status: allowed\n  training_on_outputs: forbidden\n"
                        "  source_url: https://example.com\n  auth_flow: x\n", encoding="utf-8")
    reg = load_registry(override)
    with pytest.raises(ProviderBlocked, match="BYOS allowlist"):
        require_usable("anthropic-subscription", reg)
    assert "anthropic-subscription" not in R.BYOS_ALLOWLIST
    codex_bad = tmp_path / "c.yaml"
    codex_bad.write_text("openai-codex-cli:\n  provider: openai\n  mode: byos\n  auth_kind: shared_login\n"
                         "  byos_status: allowed\n  training_on_outputs: forbidden\n", encoding="utf-8")
    with pytest.raises(ProviderBlocked, match="official kind"):
        require_usable("openai-codex-cli", load_registry(codex_bad))
    assert require_usable("openai-codex-cli")["auth_kind"] == "official_cli"  # the real entry still passes


def test_verify_registry_command_fails_the_release_check(tmp_path, capsys):
    assert cli.main(["verify-registry"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.main(["verify-registry", "--release", "--today", "2026-10-08"]) == 1  # no human sign-off yet
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and any("not verified by a human" in p for p in out["problems"])
    bad = tmp_path / "bad.yaml"
    bad.write_text("newco-api:\n  provider: newco\n  mode: api\n  auth_kind: api_key\n  byos_status: n/a\n",
                   encoding="utf-8")
    assert cli.main(["verify-registry", "--override", str(bad)]) == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert any("unknown provider 'newco'" in p for p in problems)
    assert any("missing licence flag" in p for p in problems)
