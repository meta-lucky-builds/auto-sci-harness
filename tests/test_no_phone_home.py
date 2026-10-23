"""No phone home (GOAL §17 product rules): no telemetry by default, no feedback upload, no auto-update, no runtime
package install from our code. WRITTEN, NOT RUN (lab-compute, build-only phase).

Complements tests/test_telemetry.py (which checks network-library imports and a few client names in src/*.py):
this scan also covers scripts/, docker/ and non-Python shipped files (vendored HTML/TeX assets, YAML, shell), and
adds update checks, feedback uploads, tracking endpoints and runtime installs. The scanner lives HERE, not in src/,
because test_telemetry.py fails if any src/*.py file even mentions these client names.

Fail-first reasoning: each planted line below is caught by exactly the rule named next to it; deleting that rule
makes `test_each_rule_catches_its_planted_example` fail. A repository finding fails
`test_the_repository_does_not_phone_home` and names file:line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCANNED = frozenset({".py", ".js", ".mjs", ".ts", ".tsx", ".html", ".yaml", ".yml", ".toml", ".json", ".sh", ".ps1",
                     ".tex", ".cfg", ".ini", ".Dockerfile"})
OPT_IN: dict[str, str] = {}  # "<relative path>": "<owner decision, where it is described>" (none: §17 opt-in only)
# exact literals that switch a VENDOR's reporting or self-update OFF inside the Lab box (written, never read):
# lab-agents D16 (Claude Code) and the OpenCode config in lab/agents.py
OFF_SWITCHES = {"src/custom_harness/lab/agents.py": ['"DISABLE_TELEMETRY": "1"', '"autoupdate": False']}

_SDKS = (r"posthog|sentry_sdk|mixpanel|amplitude|analytics|segment|rudderstack|rudder_analytics|datadog|ddtrace|"
         r"bugsnag|rollbar|newrelic|honeybadger|statsig|ldclient|growthbook|opentelemetry\.exporter|logfire|wandb|"
         r"comet_ml|neptune|mlflow|clearml|logrocket")
_HOSTS = (r"posthog\.com|sentry\.io|segment\.io|segment\.com/v1|mixpanel\.com|amplitude\.com|datadoghq\.|"
          r"google-analytics\.com|googletagmanager\.com|doubleclick\.net|plausible\.io|rudderstack\.com|"
          r"bugsnag\.com|rollbar\.com|nr-data\.net|newrelic\.com|statsig\.com|launchdarkly\.com|api\.wandb\.ai|"
          r"logfire[-.a-z]*\.pydantic\.dev|honeycomb\.io|lightstep\.com|app\.heap\.io|clarity\.ms")
RULES = (
    ("sdk-import", re.compile(rf"^\s*(?:import|from)\s+(?:{_SDKS})\b", re.I), {".py"}),
    ("sdk-import", re.compile(r"""(?:require\(|from\s+)['"](?:posthog-js|@sentry/|mixpanel|@amplitude/|@segment/|"""
                              r"""@datadog/|logrocket|@vercel/analytics)""", re.I), {".js", ".mjs", ".ts", ".tsx",
                                                                                    ".html"}),
    ("host", re.compile(rf"(?:{_HOSTS})", re.I), None),
    ("endpoint", re.compile(r"""https?://[^\s'"]*(?:telemetry|/v1/track|/i/v0/e/|/api/event\b)""", re.I), None),
    ("call", re.compile(r"\b(?:send_telemetry|telemetry_enabled|track_event|capture_event|report_usage|"
                        r"usage_ping)\b", re.I), None),
    ("feedback", re.compile(r"\b(?:submit_feedback|send_feedback|upload_feedback|feedback_upload)\b|"
                            r"/api/feedback\b|/v\d+/feedback\b", re.I), None),
    ("update", re.compile(r"releases/latest|pypi\.org/pypi/[^/\s'\"]+/json|api\.github\.com/repos/[^\s'\"]+/"
                          r"releases|\b(?:auto_?update|self_?update|check_for_updates?|update_check|autoupdater?)\b",
                          re.I), None),
    ("install", re.compile(r"""['"]pip['"]\s*,\s*['"]install['"]|-m\s+pip\s+install|\bensurepip\b|"""
                           r"""['"]uv['"]\s*,\s*['"]pip['"]""", re.I), {".py"}),
)


def _suffix(p: Path) -> str:
    return ".Dockerfile" if p.name.endswith(".Dockerfile") or p.name == "Dockerfile" else p.suffix.lower()


def scan_text(rel: str, text: str, suffix: str) -> list[str]:
    for lit in OFF_SWITCHES.get(rel, []):
        text = text.replace(lit, "")
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for rule, pat, only in RULES:
            if (only is None or suffix in only) and pat.search(line):
                out.append(f"{rel}:{n}: {rule}: {line.strip()[:160]}")
    return out


def scan(root: Path) -> list[str]:
    found: list[str] = []
    for sub in ("src", "scripts", "docker"):
        for p in sorted((root / sub).rglob("*")) if (root / sub).is_dir() else []:
            if not p.is_file() or "__pycache__" in p.parts or _suffix(p) not in SCANNED:
                continue
            rel = p.relative_to(root).as_posix()
            if rel in OPT_IN:
                continue
            try:
                found += scan_text(rel, p.read_text(encoding="utf-8"), _suffix(p))
            except UnicodeDecodeError:
                continue
    return found


def test_the_repository_does_not_phone_home():
    files = [p for p in (ROOT / "src").rglob("*") if p.is_file()]
    assert len(files) > 100  # the scan covers the package
    assert scan(ROOT) == []


@pytest.mark.parametrize(("rule", "suffix", "line"), [
    ("sdk-import", ".py", "import sentry_sdk"),
    ("sdk-import", ".py", "from posthog import Posthog"),
    ("sdk-import", ".js", "import x from 'posthog-js'"),
    ("host", ".html", '<script src="https://www.googletagmanager.com/gtag/js"></script>'),
    ("endpoint", ".py", "URL = 'https://api.example.com/v1/track'"),
    ("call", ".py", "send_telemetry(event)"),
    ("feedback", ".py", "client.submit_feedback(body)"),
    ("update", ".py", "requests.get('https://api.github.com/repos/o/r/releases/latest')"),
    ("update", ".py", "def check_for_updates():"),
    ("update", ".sh", "curl https://pypi.org/pypi/custom-harness/json"),
    ("install", ".py", "subprocess.run([sys.executable, '-m', 'pip', 'install', 'modal'])"),
    ("install", ".py", "subprocess.run(['pip', 'install', 'x'])"),
])
def test_each_rule_catches_its_planted_example(rule, suffix, line):
    found = scan_text("planted" + suffix, line + "\n", suffix)
    assert any(f": {rule}: " in f for f in found), (rule, line, found)


def test_prose_is_not_a_finding():
    for line in ("evolution feedback is ledgered", "self.update(other)", "an analytics-free design", "pip is pinned"):
        assert scan_text("x.py", line, ".py") == []


def test_the_off_switch_exemption_is_exact():
    rel = "src/custom_harness/lab/agents.py"
    assert scan_text(rel, 'env = {"DISABLE_TELEMETRY": "1"}', ".py") == []
    assert scan_text(rel, "send_telemetry(x)", ".py") != []
