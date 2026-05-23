"""Remove ``±img=...±`` segments whose remote URL is known dead.

The image audit (``audit_image_urls.py``) found 70+ remote URLs in
``bot/rules/main.ling`` that no longer resolve (s1.ax1x.com tomb,
klizi.cn 404s, xiaobapi 404s, xingzhige 403s, etc.). When a rule
contains such a URL, NapCat tries to fetch it before sending the QQ
message and stalls on TLS timeout — wedging the session lock and
delaying every subsequent command.

This script *removes* the offending lines from the rule file. We
delete entire lines rather than just the ``±img=...±`` part because
QRDic emits each ``±img±`` on its own line; the surrounding text /
flow stays intact. We also collapse runs of newly-empty lines so
the file stays readable.

Live URLs (qlogo.cn avatars, working APIs) are left untouched — they
are listed in ``ALIVE`` and matched first as a guard.

Usage:
    uv run python scripts/strip_dead_image_urls.py [--dry-run]

By default writes back to ``bot/rules/main.ling``. ``--dry-run`` just
prints the diff stat.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "bot" / "rules" / "main.ling"

# Hosts/prefixes that are ALIVE per the latest audit. Any image URL
# starting with one of these is preserved. Everything else that looks
# like a remote URL gets stripped.
ALIVE_PREFIXES: tuple[str, ...] = (
    "https://q1.qlogo.cn",
    "https://q2.qlogo.cn",
    "https://wkphoto.cdn.bcebos.com",
    "https://api.xingzhige.com/API/dingqiu",
)

# Templated URLs that the audit couldn't probe (contain ${var}-style
# placeholders) but we know route through alive hosts. Keep them.
TEMPLATED_KEEP_HOSTS: tuple[str, ...] = (
    "qlogo.cn",
    "xingzhige.com/API/dingqiu",
)

_IMG_LINE_RE = re.compile(r"±img=([^±]+)±")


def url_is_alive(url: str) -> bool:
    if any(url.startswith(p) for p in ALIVE_PREFIXES):
        return True
    if any(host in url for host in TEMPLATED_KEEP_HOSTS):
        return True
    return False


def is_local_ref(url: str) -> bool:
    """Local file refs (filesystem paths / @pic shorthand) bypass NapCat's URL fetch.

    These never trigger a TLS timeout — NapCat reads the file directly
    when the path resolves to a real local file (which the bot's image
    audit verified for our ``picture/*.jpg`` set). Keep them.
    """
    raw = url.strip()
    if raw.startswith(("@pic:", "/storage/", "file://")):
        return True
    return False


# Templated URLs (with ``%var%`` or ``$tool ...$``) are kept iff their
# host matches an alive prefix. The audit script can't probe these
# because the variables aren't resolved, but if the host itself is
# dead they'll fail at runtime just like the static dead URLs.
DEAD_HOSTS_FRAGMENTS: tuple[str, ...] = (
    "s1.ax1x.com",
    "klizi.cn",
    "xiaobapi.top",
    "ovooa.com/API/sho_u",  # ovooa subpaths; "ovooa.com" might still work
    "ovooa.muban.plus",
    "tianyi.qrspeed.pro",
    "api.klizi.cn",
    "ali2.a.yximgs.com",
    "js2.a.yximgs.com",
    "api.xingzhige.com/API/baororo",
    "api.xingzhige.com/API/bite",
    "api.xingzhige.com/API/grab",
    "api.xingzhige.com/API/paigua",
)


def has_dead_host(url: str) -> bool:
    return any(frag in url for frag in DEAD_HOSTS_FRAGMENTS)


def should_strip(url: str) -> bool:
    """Decide whether to drop the line carrying this URL."""
    if is_local_ref(url):
        return False
    # Templated URLs we judge by host: live host → keep, dead host → drop,
    # unknown → keep (can't be sure).
    if "%" in url or "$" in url:
        return has_dead_host(url)
    if not url.startswith(("http://", "https://", "//")):
        return False  # unknown shape, leave alone
    if url_is_alive(url):
        return False
    return True


def transform(src: str) -> tuple[str, int, list[str]]:
    """Return (new_text, lines_removed, samples)."""
    samples: list[str] = []
    out: list[str] = []
    removed = 0

    for line in src.splitlines(keepends=True):
        match = _IMG_LINE_RE.search(line)
        if match is None:
            out.append(line)
            continue
        url = match.group(1).strip()
        if should_strip(url):
            removed += 1
            if len(samples) < 10:
                samples.append(url)
            # Drop the whole line.
            continue
        out.append(line)

    new_text = "".join(out)
    # Collapse runs of >=3 blank lines into 2.
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    return new_text, removed, samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not RULES.exists():
        print(f"missing {RULES}", file=sys.stderr)
        return 2

    src = RULES.read_text(encoding="utf-8")
    new, removed, samples = transform(src)

    print(f"removed {removed} lines containing dead image URLs")
    if samples:
        print("sample of removed URLs:")
        for s in samples:
            print(f"  - {s}")

    if args.dry_run:
        print("(dry-run; no write)")
        return 0

    if new == src:
        print("nothing to change")
        return 0

    RULES.write_text(new, encoding="utf-8")
    print(f"wrote {RULES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
