import pytest

from app.ingest.severity import compute_severity, is_production, severity_rank


@pytest.mark.parametrize(
    ("exception_type", "expected"),
    [
        ("MemoryError", "critical"),
        ("sqlalchemy.exc.OperationalError", "critical"),
        ("TypeError", "high"),
        ("AttributeError", "high"),
        ("KeyError", "high"),
        ("ValidationError", "medium"),
        ("ValueError", "medium"),
        ("CustomBusinessWarning", "low"),
    ],
)
def test_base_level_by_exception_type(exception_type, expected):
    assert compute_severity(exception_type, production=False, event_count=1) == expected


def test_severity_is_deterministic():
    results = {compute_severity("TypeError", True, 5) for _ in range(20)}
    assert results == {"critical"}


def test_type_error_in_production_ranks_higher_than_in_development():
    production = compute_severity("TypeError", is_production("production"), 1)
    development = compute_severity("TypeError", is_production("development"), 1)
    assert severity_rank(production) > severity_rank(development)
    assert (development, production) == ("high", "critical")


def test_severity_rises_when_frequency_thresholds_are_crossed():
    assert compute_severity("ValueError", False, 9) == "medium"
    assert compute_severity("ValueError", False, 10) == "high"
    assert compute_severity("ValueError", False, 99) == "high"
    assert compute_severity("ValueError", False, 100) == "critical"
    assert compute_severity("Warning", False, 9) == "low"
    assert compute_severity("Warning", False, 10) == "medium"


def test_severity_is_capped_at_critical():
    assert compute_severity("MemoryError", True, 1000) == "critical"


def test_production_aliases():
    assert is_production("Production")
    assert is_production("prod")
    assert not is_production("staging")
