"""Safety rules around paper motion.

A scan request must never move media on the operator's behalf.  The
Unload -> Load recovery cycle feeds ``unload`` at full roller speed with no
tension control; on a long sheet that walks the media sideways into the side
guides and tears it (observed 2026-11-02 on a 30 ft sheet).  Recovery is a
manual, watched action: the app stops and asks.

These tests pin that rule down so a future refactor cannot quietly reintroduce
an automatic recovery.
"""
from __future__ import annotations

import pytest

from ctx.engine import require_manual_recovery


def test_mid_path_media_raises_instead_of_moving() -> None:
    with pytest.raises(RuntimeError) as exc:
        require_manual_recovery(already_ready=False, recover=False, state="SCANNING")
    message = str(exc.value)
    assert "BY HAND" in message
    # The operator must be told nothing moved, so they know the sheet is untouched.
    assert "No paper was moved" in message


def test_ready_media_is_allowed() -> None:
    require_manual_recovery(already_ready=True, recover=False, state="READY")


def test_explicit_opt_in_is_allowed_but_never_default() -> None:
    # Only an explicit caller request may run the cycle.
    require_manual_recovery(already_ready=False, recover=True, state="SCANNING")
