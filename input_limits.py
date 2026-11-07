"""Shared input budgets, optionally overridden by local input-limits.json."""
import json
from pathlib import Path

MAX_FILES = 32
MAX_PROMPT_CHARS = 65536
MAX_PACKED_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 32 * 1024 * 1024
MAX_HTTP_BYTES = 4 * 1024 * 1024

DEFAULT_LIMITS = {
    "MAX_FILES": MAX_FILES,
    "MAX_PROMPT_CHARS": MAX_PROMPT_CHARS,
    "MAX_PACKED_BYTES": MAX_PACKED_BYTES,
    "MAX_FILE_BYTES": MAX_FILE_BYTES,
    "MAX_TOTAL_FILE_BYTES": MAX_TOTAL_FILE_BYTES,
    "MAX_HTTP_BYTES": MAX_HTTP_BYTES,
}
# Bound memory/disk use even when a local configuration is accidentally mistyped.
CEILINGS = {
    "MAX_FILES": 256,
    "MAX_PROMPT_CHARS": 1024 * 1024,
    "MAX_PACKED_BYTES": 16 * 1024 * 1024,
    "MAX_FILE_BYTES": 64 * 1024 * 1024,
    "MAX_TOTAL_FILE_BYTES": 256 * 1024 * 1024,
    "MAX_HTTP_BYTES": 32 * 1024 * 1024,
}


def load_limits(root=None):
    """Return a fresh validated budget dict; absent configuration uses defaults.

    Pass the bridge root explicitly when running an isolated copy or test.
    Values count Unicode characters for MAX_PROMPT_CHARS and bytes otherwise.
    """
    path = Path(root) / "input-limits.json" if root is not None else Path(__file__).with_name("input-limits.json")
    limits = dict(DEFAULT_LIMITS)
    if path.exists():
        try:
            configured = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("Invalid input-limits.json") from error
        if not isinstance(configured, dict) or set(configured) - set(limits):
            raise ValueError("input-limits.json contains unsupported limits")
        for key, value in configured.items():
            if type(value) is not int or not 1 <= value <= CEILINGS[key]:
                raise ValueError(f"{key} must be an integer between 1 and {CEILINGS[key]}")
        limits.update(configured)
    if limits["MAX_TOTAL_FILE_BYTES"] < limits["MAX_FILE_BYTES"]:
        raise ValueError("MAX_TOTAL_FILE_BYTES must be at least MAX_FILE_BYTES")
    return limits
