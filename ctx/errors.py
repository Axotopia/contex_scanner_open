"""Errors, SCSI status classification, and the recovery action for each.

``STATUS.md`` §1 lists the status/sense combinations that have actually been
observed; ``HANDOFF-COMMS.md`` §6.2 item 4 asks for the *error -> action* table
that is missing. This module is that table, kept explicit so the session layer
never has to guess what to do with a failure.
"""
from __future__ import annotations

from enum import Enum


class ContexError(Exception):
    """Base class for every error raised by this package."""


class DiscoveryError(ContexError):
    """The scanner did not answer, or answered with something unparseable."""


class TransportError(ContexError):
    """TCP/UDP failure: refused, reset, timed out, or closed mid-transaction."""


class ProtocolError(ContexError):
    """The peer sent bytes that violate the recovered wire format."""


class SafetyViolation(ContexError):
    """A command was refused because it could commit to scanner flash.

    ``DESIGN.md`` §1 and ``HANDOFF-COMMS.md`` §4: no firmware image exists
    anywhere in the payloads we hold, so a partial flash write has no documented
    recovery. These commands are refused before they reach the socket.
    """


class Action(Enum):
    """What the session should do about a non-GOOD status."""

    OK = "ok"
    RETRY = "retry"
    RETRY_READY = "retry_until_ready"
    CLEAR_RESERVATION = "clear_reservation"
    FIX_CDB = "fix_cdb"
    FIX_PARAMETERS = "fix_parameters"
    ABORT = "abort"


class CommandFailed(ContexError):
    """A command returned a non-GOOD status.

    Carries the raw status triple plus the action the caller should take, so
    the failure is diagnosable from the exception alone.
    """

    def __init__(self, opcode: int, status: int, sense_key: int, asc: int, ascq: int,
                 action: Action, detail: str = ""):
        self.opcode = opcode
        self.status = status
        self.sense_key = sense_key
        self.asc = asc
        self.ascq = ascq
        self.action = action
        self.detail = detail
        super().__init__(
            f"opcode 0x{opcode:02x} failed: status=0x{status:02x} "
            f"sense_key=0x{sense_key:02x} ASC=0x{asc:02x} ASCQ=0x{ascq:02x} "
            f"-> action={action.value}" + (f" ({detail})" if detail else "")
        )

    @classmethod
    def from_sense(cls, opcode: int, sense) -> "CommandFailed":
        return cls(opcode, sense.status, sense.sense_key, sense.asc, sense.ascq,
                   sense.action, sense.describe_hint())
