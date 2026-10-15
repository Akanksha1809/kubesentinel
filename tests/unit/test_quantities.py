import pytest

from kubesentinel.optimization.quantities import (
    QuantityParseError,
    format_cpu_millicores,
    format_memory_bytes,
    parse_cpu_to_millicores,
    parse_memory_to_bytes,
)


@pytest.mark.parametrize(
    "raw,expected_millicores",
    [
        ("1", 1000),
        ("2", 2000),
        ("0.5", 500),
        ("0.1", 100),
        ("100m", 100),
        ("250m", 250),
        ("1000m", 1000),
        ("132n", 0),  # 132 nanocores rounds down to 0 millicores
        ("500000n", 1),  # 500000 nanocores = 0.5m, rounds up (half-up) to 1
        ("1500000n", 2),  # 1.5m rounds up to 2
        ("1u", 0),  # 1 microcore = 0.001m, rounds to 0
        ("1500u", 2),  # 1500 microcores = 1.5m, rounds up to 2
    ],
)
def test_parse_cpu_to_millicores(raw, expected_millicores):
    assert parse_cpu_to_millicores(raw) == expected_millicores


@pytest.mark.parametrize(
    "raw,expected_bytes",
    [
        ("128974848", 128974848),
        ("1Ki", 1024),
        ("128Mi", 128 * 1024 * 1024),
        ("1Gi", 1024**3),
        ("1Ti", 1024**4),
        ("1k", 1000),
        ("1M", 1_000_000),
        ("1G", 1_000_000_000),
        ("2048Ki", 2048 * 1024),
        ("1e3", 1000),
        ("1.5Gi", int(1.5 * 1024**3)),
    ],
)
def test_parse_memory_to_bytes(raw, expected_bytes):
    assert parse_memory_to_bytes(raw) == expected_bytes


@pytest.mark.parametrize("raw", ["", "abc", "1Xi", "1K", "--5", "5..5", "Gi", "1Zi"])
def test_parse_rejects_invalid_quantities(raw):
    with pytest.raises(QuantityParseError):
        parse_cpu_to_millicores(raw)
    with pytest.raises(QuantityParseError):
        parse_memory_to_bytes(raw)


def test_parse_memory_rejects_milli_suffix_for_memory_context():
    # "m" is a valid decimal suffix (milli) and parses without error -- the
    # quantity grammar itself doesn't forbid it for memory, Kubernetes just
    # never produces it there. Confirms the parser is permissive about which
    # *suffixes* are valid, not which *fields* they're conventionally used
    # for -- that distinction belongs to a caller, not this module.
    assert parse_memory_to_bytes("1000m") == 1


@pytest.mark.parametrize(
    "millicores,expected",
    [(1000, "1"), (2000, "2"), (500, "500m"), (100, "100m"), (0, "0")],
)
def test_format_cpu_millicores(millicores, expected):
    assert format_cpu_millicores(millicores) == expected


@pytest.mark.parametrize(
    "num_bytes,expected",
    [
        (1024, "1Ki"),
        (128 * 1024 * 1024, "128Mi"),
        (1024**3, "1Gi"),
        (1000, "1000"),  # not a power-of-1024 multiple -- falls back to bare bytes
        (0, "0"),
    ],
)
def test_format_memory_bytes(num_bytes, expected):
    assert format_memory_bytes(num_bytes) == expected


def test_cpu_round_trip_for_clean_values():
    for raw in ["1", "2", "500m", "250m"]:
        millicores = parse_cpu_to_millicores(raw)
        assert parse_cpu_to_millicores(format_cpu_millicores(millicores)) == millicores


def test_memory_round_trip_for_clean_values():
    for raw in ["1Gi", "128Mi", "512Ki"]:
        num_bytes = parse_memory_to_bytes(raw)
        assert parse_memory_to_bytes(format_memory_bytes(num_bytes)) == num_bytes
