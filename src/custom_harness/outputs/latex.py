"""LaTeX compilation, ONLY inside a network-free container sandbox (GOAL §17 "Research outputs"; PORTMAP §5).

A `.tex` file is code: `\\write18` runs shell commands, `\\input{/etc/passwd}` reads host files into the PDF, a
`.latexmkrc` is Perl that latexmk executes. So a compile never runs on the host. `compile_in_sandbox` refuses any
sandbox that is not a container with the network off, and the engine is invoked with the hardened settings below.

Ported from OpenResearch (MIT, Copyright (c) 2026 alphaXiv; src/local/latex.rs at fe36f74):
- engine choice from a `% !TeX program = …` line (pdfLaTeX default), driver preference latexmk > engine directly >
  tectonic (tectonic only stands in for XeTeX) (`program_from_source`, `choose_driver`);
- argv: latexmk `-norc` (never run an agent-written .latexmkrc), `-interaction=nonstopmode`, `-no-shell-escape`,
  `-output-directory`; the source passed as `./name` so a file named `-foo.tex` is a path, not an option; two passes
  when driving the engine directly; tectonic with `--keep-logs -Z continue-on-errors` (`pass_args`, `pass_count`);
- success is judged by the log (`! ` error lines), not the exit status, because continue-on-errors exits 0 on a
  partly typeset document (`reports_errors`); the 120 s per-pass limit.
Not ported: OR's own bibtex/biber pass for the direct-engine driver (latexmk and tectonic run it themselves; a
direct pdflatex compile of a document with a bibliography leaves citations unresolved, reported only as LaTeX
warnings, not as a failure).
Ours, added: `openin_any=p`, `openout_any=p` (paranoid: no reading or writing outside the working tree, no dot
files) and `shell_escape=f` through the environment (kpathsea reads these), tectonic `--only-cached` (`-C`: the
sandbox has no network, so the bundle must be pre-cached in the image) and the sandbox requirement.
Image decision for the owner: docs/lab_compute.md "TeX image". Built, NOT test-verified (no TeX image exists yet).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ..discovery_trace import TraceSink, safe_emit
from ..sandbox.base import Sandbox, safe_path

PASS_TIMEOUT_S = 120.0
TEX_IMAGE = "custom-harness-tex:tectonic"  # docker/sandbox-tex.Dockerfile (NOT built)
SAFE_TEX_ENV = {"openin_any": "p", "openout_any": "p", "shell_escape": "f", "HOME": "/tmp"}
_PROGRAM_LINE = re.compile(r"^\s*%\s*!\s*TeX\s+(?:TS-)?program\s*=\s*(\S+)", re.IGNORECASE)
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}\.tex")


class Program(Enum):
    PDF = "pdflatex"
    XE = "xelatex"
    LUA = "lualatex"

    @property
    def latexmk_flag(self) -> str:
        return {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}[self.value]

    @property
    def served_by_tectonic(self) -> bool:
        return self is Program.XE


class Driver(Enum):
    LATEXMK = "latexmk"
    DIRECT = "direct"
    TECTONIC = "tectonic"


def program_from_source(text: str, head_lines: int = 20) -> Program | None:
    for line in text.splitlines()[:head_lines]:
        m = _PROGRAM_LINE.match(line)
        if m:
            name = m.group(1).lower()
            for p, names in ((Program.PDF, ("pdflatex", "pdflatexmk")), (Program.XE, ("xelatex", "xelatexmk")),
                             (Program.LUA, ("lualatex", "lualatexmk"))):
                if name in names:
                    return p
    return None


def choose_driver(program: Program, available: Callable[[str], bool]) -> Driver | None:
    if available("latexmk") and available(program.value):
        return Driver.LATEXMK
    if available(program.value):
        return Driver.DIRECT
    if available("tectonic"):
        return Driver.TECTONIC
    return None


def pass_args(driver: Driver, program: Program, outdir: str, name: str) -> list[str]:
    if not _NAME.fullmatch(name):
        raise ValueError(f"refusing source name {name!r}")
    source = f"./{name}"
    if driver is Driver.LATEXMK:
        return ["latexmk", "-norc", program.latexmk_flag, "-interaction=nonstopmode", "-no-shell-escape",
                f"-output-directory={outdir}", source]
    if driver is Driver.DIRECT:
        return [program.value, "-interaction=nonstopmode", "-no-shell-escape", f"-output-directory={outdir}", source]
    return ["tectonic", "--only-cached", "--outdir", outdir, "--keep-logs", "-Z", "continue-on-errors", source]


def pass_count(driver: Driver) -> int:
    return 2 if driver is Driver.DIRECT else 1


def reports_errors(log: str) -> bool:
    return any(line.startswith("! ") for line in log.splitlines())


class CompileRefused(PermissionError):
    """LaTeX may only be compiled in a network-free container sandbox."""


def require_isolated_sandbox(sandbox: Sandbox) -> None:
    from ..sandbox.local import LocalSandbox
    if isinstance(sandbox, LocalSandbox) or "ssh" in type(sandbox).__name__.lower():
        raise CompileRefused(f"{type(sandbox).__name__} is not a container: LaTeX is never compiled on a host")
    if getattr(sandbox, "network", None) is not False:
        raise CompileRefused("the compile sandbox must have its network OFF")


@dataclass(frozen=True)
class CompileResult:
    ok: bool
    pdf: bytes | None
    log: str
    driver: str
    problems: tuple[str, ...]


def compile_in_sandbox(sandbox: Sandbox, name: str, *, available: Callable[[str], bool],
                       outdir: str = "out", trace: TraceSink | None = None) -> CompileResult:
    """Compile `name` (a .tex file already in the sandbox workspace). `available(tool)` says which TeX tools the
    sandbox image has (known from the image, never probed on the host)."""
    require_isolated_sandbox(sandbox)
    src = safe_path(sandbox.workspace, name)
    text = src.read_text(encoding="utf-8", errors="replace")
    program = program_from_source(text) or Program.PDF
    driver = choose_driver(program, available)
    if driver is None:
        return CompileResult(False, None, "", "none", (f"no TeX driver for {program.value} in the image",))
    safe_path(sandbox.workspace, outdir).mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    notes = "" if driver is not Driver.TECTONIC or program.served_by_tectonic else (
        f"[compiled with tectonic (XeTeX) although the document asks for {program.value}]\n")
    out = ""
    for _ in range(pass_count(driver)):
        r = sandbox.exec(pass_args(driver, program, outdir, name), timeout_s=PASS_TIMEOUT_S, env=dict(SAFE_TEX_ENV))
        out += r.stdout + r.stderr
        if r.timed_out:
            problems.append(f"a {driver.value} pass exceeded {PASS_TIMEOUT_S:.0f} s")
            break
    stem = name[:-4]
    log_path = safe_path(sandbox.workspace, f"{outdir}/{stem}.log")
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else out
    if reports_errors(log):
        problems.append("the TeX log reports errors ('! ' lines)")
    pdf_path = safe_path(sandbox.workspace, f"{outdir}/{stem}.pdf")
    pdf = pdf_path.read_bytes() if pdf_path.is_file() else None
    if pdf is None:
        problems.append("no PDF was produced")
    result = CompileResult(not problems, pdf, notes + log[-20000:], driver.value, tuple(problems))
    safe_emit(trace, "output.compiled", name, {"driver": driver.value, "ok": result.ok, "problems": list(problems),
                                               "pdf_sha256": hashlib.sha256(pdf).hexdigest() if pdf else None})
    return result
