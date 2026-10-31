"""Byte layouts for the Contex SCSI-over-TCP/UDP protocol.

Pure, socket-free functions and records: everything in this subpackage can be
tested against captured traffic without a scanner attached.
"""
from . import cdb, image, sense, window  # noqa: F401
