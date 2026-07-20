#!/usr/bin/env python3
"""Guard: make sure the built app.yml keeps its runtime `env:` block.

WHY THIS EXISTS: Databricks Apps reads runtime env from app.yml (NOT from
databricks.yml config.env). `apx build` regenerates .build/app.yml and has been
observed to drop everything except the uvicorn `command`. When the env block is
gone, DATABRICKS_WAREHOUSE_ID is unset -> db.py raises -> every /api endpoint
500s -> the dashboard is blank. This has bitten us before.

This script runs right after `apx build`. If the built app.yml is missing the
required env keys, it copies the `env:` block from the source app.yml (the
source of truth we maintain) into the build output. If it can't (no source env
either), it exits non-zero so the deploy fails loudly instead of shipping a
blank app.

Idempotent. No third-party deps (uses a tiny hand-rolled parse of the two
top-level keys we care about) so it runs anywhere the build runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

REQUIRED = "DATABRICKS_WAREHOUSE_ID"
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "app.yml"
BUILT = ROOT / ".build" / "app.yml"


def has_env(text: str) -> bool:
    # Cheap structural check: an `env:` key with our required var under it.
    return "env:" in text and REQUIRED in text


def extract_env_block(text: str) -> str | None:
    """Return the top-level `env:` block (the `env:` line + its indented body)."""
    lines = text.splitlines()
    out: list[str] = []
    grabbing = False
    for line in lines:
        if line.rstrip() == "env:" or line.startswith("env:"):
            grabbing = True
            out.append(line)
            continue
        if grabbing:
            # Block continues while the line is blank or indented (part of env:).
            if line.strip() == "" or line[:1] in (" ", "\t"):
                out.append(line)
            else:
                break
    block = "\n".join(out).rstrip()
    return block if block and REQUIRED in block else None


def main() -> int:
    if not BUILT.exists():
        print(f"[ensure_app_env] {BUILT} not found — did `apx build` run?", file=sys.stderr)
        return 1
    built = BUILT.read_text()
    if has_env(built):
        print("[ensure_app_env] OK — built app.yml has its env block.")
        return 0
    print("[ensure_app_env] built app.yml is MISSING its env block — repairing from source.")
    if not SRC.exists():
        print(f"[ensure_app_env] no source {SRC} to repair from — failing.", file=sys.stderr)
        return 1
    block = extract_env_block(SRC.read_text())
    if not block:
        print("[ensure_app_env] source app.yml has no usable env block — failing.", file=sys.stderr)
        return 1
    BUILT.write_text(built.rstrip() + "\n" + block + "\n")
    print("[ensure_app_env] repaired: re-injected env block into built app.yml.")
    # Re-verify.
    return 0 if has_env(BUILT.read_text()) else 1


if __name__ == "__main__":
    sys.exit(main())
