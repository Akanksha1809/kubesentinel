"""Kubernetes resource quantity parsing and formatting.

The `kubernetes` Python client has no equivalent of client-go's
`resource.Quantity` -- confirmed by reading the installed client (30.1.0);
there is no such helper anywhere in it. Every CPU/memory string this project
sees (Pod spec requests/limits, Node allocatable, Metrics API usage samples)
is parsed here, exactly once, into a normalized integer unit:

- CPU -> millicores (int). Kubernetes' own smallest addressable CPU unit is
  1m, so sub-millicore precision is never meaningful; values are rounded to
  the nearest millicore, never truncated (truncating "5.0004 cores" worth of
  input down would silently under-report real usage).
- Memory -> bytes (int). Memory quantities are always exact in practice
  (Ki/Mi/Gi or bare bytes); `Decimal` is used throughout so no float
  imprecision is introduced even for the rarer decimal-SI/exponent forms.

Parsing uses `decimal.Decimal`, never `float`, specifically because
Kubernetes' own quantity type (Go's `inf.Dec`) is exact decimal arithmetic --
converting through a binary float at any point in this pipeline would be a
silent precision bug, not a style choice.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Decimal SI suffixes (multiply by 1000^n): nano, micro, milli, "" (ones),
# kilo, mega, giga, tera, peta, exa. Kubernetes never uses capital "K" alone
# (only the binary "Ki") -- rejecting it rather than guessing is deliberate;
# see the module docstring on never silently accepting malformed input.
_DECIMAL_EXPONENTS: dict[str, int] = {
    "n": -9, "u": -6, "m": -3, "": 0,
    "k": 3, "M": 6, "G": 9, "T": 12, "P": 15, "E": 18,
}
# Binary SI suffixes (multiply by 1024^n).
_BINARY_EXPONENTS: dict[str, int] = {
    "Ki": 10, "Mi": 20, "Gi": 30, "Ti": 40, "Pi": 50, "Ei": 60,
}

_QUANTITY_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)$")

_MILLICORES_PER_CORE = Decimal(1000)


class QuantityParseError(ValueError):
    """Raised on any string that isn't a valid Kubernetes resource quantity.
    Never caught and silently coerced to zero or None by this module --
    callers decide how to handle a parse failure, but it is always visible."""


def _parse_decimal_value(raw: str) -> Decimal:
    match = _QUANTITY_RE.match(raw.strip())
    if not match:
        raise QuantityParseError(f"not a valid Kubernetes quantity: {raw!r}")
    number_part, suffix = match.groups()
    try:
        number = Decimal(number_part)
    except InvalidOperation as exc:
        raise QuantityParseError(f"not a valid Kubernetes quantity: {raw!r}") from exc

    if suffix in _BINARY_EXPONENTS:
        return number * (Decimal(2) ** _BINARY_EXPONENTS[suffix])
    if suffix in _DECIMAL_EXPONENTS:
        return number * (Decimal(10) ** _DECIMAL_EXPONENTS[suffix])
    raise QuantityParseError(f"unrecognized quantity suffix {suffix!r} in {raw!r}")


def parse_cpu_to_millicores(value: str) -> int:
    """Parses a CPU quantity (e.g. "2", "0.5", "100m", "132n" as metrics-server
    reports usage) into whole millicores, rounded to the nearest one."""
    cores = _parse_decimal_value(value)
    millicores = cores * _MILLICORES_PER_CORE
    return int(millicores.to_integral_value(rounding="ROUND_HALF_UP"))


def parse_memory_to_bytes(value: str) -> int:
    """Parses a memory quantity (e.g. "128Mi", "1Gi", "512000000", "2048Ki")
    into whole bytes, rounded to the nearest one (sub-byte input is not
    meaningful but is still rounded rather than truncated for consistency)."""
    byte_value = _parse_decimal_value(value)
    return int(byte_value.to_integral_value(rounding="ROUND_HALF_UP"))


# Preferred binary units for human-readable formatting, largest first.
_MEMORY_FORMAT_UNITS: list[tuple[str, int]] = [
    ("Ei", 60), ("Pi", 50), ("Ti", 40), ("Gi", 30), ("Mi", 20), ("Ki", 10),
]


def format_cpu_millicores(millicores: int) -> str:
    """Formats millicores back into the conventional Kubernetes CPU string:
    whole cores with no suffix when it divides evenly, "<n>m" otherwise --
    matching how `kubectl` itself displays CPU quantities."""
    if millicores % 1000 == 0:
        return str(millicores // 1000)
    return f"{millicores}m"


def format_memory_bytes(num_bytes: int) -> str:
    """Formats bytes back into the largest binary unit that divides evenly,
    falling back to bare bytes -- matching how `kubectl` itself displays
    memory quantities (binary units, not decimal, since that's what
    Kubernetes manifests conventionally use)."""
    for suffix, exponent in _MEMORY_FORMAT_UNITS:
        unit = 1 << exponent
        if num_bytes != 0 and num_bytes % unit == 0:
            return f"{num_bytes // unit}{suffix}"
    return str(num_bytes)
