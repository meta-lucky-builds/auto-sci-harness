"""Check that the offline human draw secret (GOAL §16.3) appears nowhere agents can read, scratchpads included.

    CHH_DRAW_SECRET_FILE=<path outside every agent-readable root> python scripts/check_secret.py [ROOT ...]

Roots: the arguments, else $CHH_AGENT_READABLE (os.pathsep-separated), else commitment.default_agent_readable_roots().
Exit 0 = clean, 1 = the secret (raw, hex, base64) or the secret file was found under a root, or a file could not be
read, 2 = no usable secret configured. Prints paths only, never the secret.
"""

from __future__ import annotations

import sys
from pathlib import Path

from custom_harness.commitment import DrawError, default_agent_readable_roots, load_secret, secret_leak_scan


def main(argv: list[str]) -> int:
    import os

    from custom_harness.commitment import SECRET_FILE_ENV

    try:
        secret = load_secret()
    except (DrawError, OSError) as e:
        print(f"refused: {type(e).__name__}: {e}")
        return 2
    roots = [Path(a) for a in argv] or default_agent_readable_roots()
    problems = secret_leak_scan(secret, roots, secret_file=Path(os.environ[SECRET_FILE_ENV]))
    for p in problems:
        print(p)
    print(f"scanned {len(roots)} root(s): {'LEAK' if problems else 'clean'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
