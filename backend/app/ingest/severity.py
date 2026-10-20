"""Deterministic incident severity rules.

Levels, lowest to highest: ``low``, ``medium``, ``high``, ``critical``.

1. Base level by exception type (the last dotted component is used, so
   ``sqlalchemy.exc.OperationalError`` matches ``OperationalError``):

   - critical: ``MemoryError`` and database connection errors
     (``OperationalError``, ``InterfaceError``, ``DatabaseError``,
     ``DatabaseConnectionError``, ``DBConnectionError``).
   - high: unhandled exceptions that surface as HTTP 5xx: ``TypeError``,
     ``AttributeError``, ``KeyError``, ``IndexError``, ``LookupError``,
     ``NameError``, ``UnboundLocalError``, ``ZeroDivisionError``,
     ``RuntimeError``, ``RecursionError``, ``AssertionError``,
     ``NotImplementedError``.
   - medium: input errors: ``ValidationError``, ``RequestValidationError``,
     ``ValueError``, ``UnicodeDecodeError``.
   - low: anything else.

2. +1 level when the incident has seen an event from the ``production``
   environment (``prod`` is accepted as an alias, case-insensitive).

3. +1 level for each frequency threshold reached by the incident's event
   count: ``>= 10`` and ``>= 100``.

4. The result is capped at ``critical``.
"""

SEVERITY_LEVELS = ("low", "medium", "high", "critical")
FREQUENCY_THRESHOLDS = (10, 100)
PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})

_CRITICAL_TYPES = frozenset(
    {
        "MemoryError",
        "OperationalError",
        "InterfaceError",
        "DatabaseError",
        "DatabaseConnectionError",
        "DBConnectionError",
    }
)
_HIGH_TYPES = frozenset(
    {
        "TypeError",
        "AttributeError",
        "KeyError",
        "IndexError",
        "LookupError",
        "NameError",
        "UnboundLocalError",
        "ZeroDivisionError",
        "RuntimeError",
        "RecursionError",
        "AssertionError",
        "NotImplementedError",
    }
)
_MEDIUM_TYPES = frozenset(
    {"ValidationError", "RequestValidationError", "ValueError", "UnicodeDecodeError"}
)


def is_production(environment: str) -> bool:
    return environment.strip().lower() in PRODUCTION_ENVIRONMENTS


def base_level(exception_type: str) -> int:
    name = exception_type.strip().rsplit(".", 1)[-1]
    if name in _CRITICAL_TYPES:
        return 3
    if name in _HIGH_TYPES:
        return 2
    if name in _MEDIUM_TYPES:
        return 1
    return 0


def compute_severity(exception_type: str, production: bool, event_count: int) -> str:
    level = base_level(exception_type)
    if production:
        level += 1
    level += sum(1 for threshold in FREQUENCY_THRESHOLDS if event_count >= threshold)
    return SEVERITY_LEVELS[min(level, len(SEVERITY_LEVELS) - 1)]


def severity_rank(severity: str) -> int:
    return SEVERITY_LEVELS.index(severity)
