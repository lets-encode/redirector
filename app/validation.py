"""Slug validation: allowlist by shape, then reserved names.

The spec's regex ^[a-z0-9]([a-z0-9-]{1,38}[a-z0-9])?$ on its own would admit
single-character names and internal double hyphens, which the prose forbids
(length 3-40, no double hyphens). The prose wins: we check length and '--'
explicitly on top of the regex.

There is deliberately no profanity/abuse blocklist: abusive names are handled
reactively by staff via the admin tombstone (see app/main.py), not filtered at
registration.
"""

from __future__ import annotations

import re

from .config import RESERVED_NAMES

SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,38}[a-z0-9])?$")

MIN_LEN = 3
MAX_LEN = 40

# Distinct rejection classes, so tests and UI copy can tell them apart.
ERR_SYNTAX = (
    "Names must be 3-40 characters: lowercase letters, digits, and single "
    "internal hyphens (no leading, trailing, or double hyphens)."
)
ERR_RESERVED = "That name is reserved and cannot be used for a campaign."


def syntax_error(name: str) -> str | None:
    """Return an error message if the name fails shape rules, else None."""
    if not isinstance(name, str) or not name:
        return ERR_SYNTAX
    if "%" in name:  # percent-encoding is rejected outright, never decoded
        return ERR_SYNTAX
    if not (MIN_LEN <= len(name) <= MAX_LEN):
        return ERR_SYNTAX
    if "--" in name:
        return ERR_SYNTAX
    if not SLUG_RE.fullmatch(name):
        return ERR_SYNTAX
    return None


def registration_error(name: str) -> str | None:
    """Full check for registering a new campaign slug: shape, then reserved
    names. Abusive names are not filtered here — see the module docstring."""
    err = syntax_error(name)
    if err:
        return err
    if name in RESERVED_NAMES:
        return ERR_RESERVED
    return None
