import pytest

from kubesentinel.observer.backoff import compute_backoff_seconds


def test_backoff_grows_exponentially():
    assert compute_backoff_seconds(0, base=1.0, cap=100.0) == 1.0
    assert compute_backoff_seconds(1, base=1.0, cap=100.0) == 2.0
    assert compute_backoff_seconds(2, base=1.0, cap=100.0) == 4.0


def test_backoff_respects_cap():
    assert compute_backoff_seconds(10, base=1.0, cap=30.0) == 30.0


def test_backoff_rejects_negative_attempt():
    with pytest.raises(ValueError):
        compute_backoff_seconds(-1)
