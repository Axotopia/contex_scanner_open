"""Packaged wire data recovered from the vendor's traffic.

``oem_recipe.json`` holds the OEM command blocks that produced a real scan: the
arming writes, the five tables, and the trigger. Each entry is a 16-byte CDB
immediately followed by its data-out payload, exactly as recorded.

The captured ``CDB[10:16]`` reservation token is preserved verbatim in the file
so it remains an honest record of what the vendor sent. **It is never emitted:**
:meth:`ctx.net.command.CommandChannel.transact` zeroes that field on every
command (``SPECS.md`` §2.3).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

RECIPE_PATH = Path(__file__).with_name("oem_recipe.json")


@lru_cache(maxsize=1)
def load_recipe() -> dict:
    with open(RECIPE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def split_entry(entry: str) -> tuple[bytes, bytes]:
    """Split a ``cdb||payload`` hex entry into ``(cdb, payload)``."""
    raw = bytes.fromhex(entry)
    if len(raw) < 16:
        raise ValueError(f"recipe entry is {len(raw)} bytes, need at least a 16-byte CDB")
    return raw[:16], raw[16:]


def command(name: str) -> tuple[bytes, bytes]:
    """Return ``(cdb, payload)`` for a named single-entry recipe command."""
    recipe = load_recipe()
    if name not in recipe:
        raise KeyError(f"no recipe entry {name!r}")
    return split_entry(recipe[name])


def tables(count: int) -> list[tuple[bytes, bytes]]:
    recipe = load_recipe()
    entries = recipe["tables"]
    if not 0 <= count <= len(entries):
        raise ValueError(f"table count must be 0..{len(entries)}, got {count}")
    return [split_entry(e) for e in entries[:count]]
