"""Portable SQL for the CHECK constraints the domain depends on.

The constraints were first written against PostgreSQL, using its ``~`` regex
operator and ``btrim``. MySQL 8 has neither, and its CHECK constraints reject
regex outright -- the engine only permits a narrow set of deterministic,
built-in expressions. These helpers emit SQL that means the same thing on both
engines, so a constraint written once is enforced identically on either.

Two rules shape the SQL here:

* No collation dependence. MySQL's default ``utf8mb4_0900_ai_ci`` collation is
  case-insensitive, so ``value = 'USD'`` would silently also accept ``usd``.
  Character comparisons therefore go through ``ASCII``, which is not collation
  sensitive, making the constraint exact on both engines.
* No disallowed functions. ``TRANSLATE`` and regular expressions are rejected
  inside a MySQL CHECK; ``ASCII``, ``SUBSTRING``, ``CHAR_LENGTH`` and
  ``REPLACE`` are accepted.

Generated rather than hand-written because the SHA-256 expression nests 16
``REPLACE`` calls, and a typo in any one of them would silently weaken the
constraint instead of failing loudly.
"""

from __future__ import annotations

_HEX_DIGITS = "0123456789abcdef"

#: ASCII codes for the literal prefix ``sha256:``. Spelled out so the prefix
#: check is a case-sensitive numeric comparison rather than a string comparison,
#: which the case-insensitive default collation would otherwise relax.
_SHA256_PREFIX_CODES = (115, 104, 97, 50, 53, 54, 58)


def not_blank(column: str) -> str:
    """Return SQL asserting ``column`` is not empty or whitespace-only.

    ``TRIM`` covers the same ground as PostgreSQL's ``btrim`` -- surrounding
    spaces removed -- and exists on both engines.
    """
    return f"TRIM({column}) <> ''"


def is_iso4217_currency(column: str) -> str:
    """Return SQL asserting ``column`` is exactly three uppercase ASCII letters.

    Character-for-character equivalent to the original ``~ '^[A-Z]{3}$'``: three
    characters, each in the ``A``-``Z`` code range, so ``usd``, ``USDX``, ``12D``
    and ``ab1`` are all rejected.
    """
    positions = " AND ".join(
        f"ASCII(SUBSTRING({column}, {index}, 1)) BETWEEN 65 AND 90" for index in (1, 2, 3)
    )
    return f"CHAR_LENGTH({column}) = 3 AND {positions}"


def is_sha256_fingerprint(column: str) -> str:
    """Return SQL asserting ``column`` is exactly ``sha256:`` plus 64 lowercase hex digits.

    Character-for-character equivalent to the original
    ``~ '^sha256:[0-9a-f]{64}$'``:

    * the literal ``sha256:`` prefix, matched case-sensitively;
    * a total length of 71, so a short or long body is rejected;
    * 64 characters drawn from ``0-9a-f``.

    The body is checked by stripping every hexadecimal character with nested
    ``REPLACE`` calls and requiring what remains to be empty. Uppercase ``A``-``F``
    are deliberately absent from the strip set, so an uppercase digest fails --
    the same rejection the original regex produced.
    """
    prefix = " AND ".join(
        f"ASCII(SUBSTRING({column}, {index}, 1)) = {code}"
        for index, code in enumerate(_SHA256_PREFIX_CODES, start=1)
    )
    body = f"SUBSTRING({column}, 8)"
    for digit in _HEX_DIGITS:
        body = f"REPLACE({body}, '{digit}', '')"
    return f"CHAR_LENGTH({column}) = 71 AND {prefix} AND CHAR_LENGTH({body}) = 0"


def nullable(column: str, condition: str) -> str:
    """Return ``condition`` guarded by ``column IS NULL OR ...``.

    Preserves the existing optional-column checks, where NULL is an accepted way
    of saying "this was never recorded" and only a non-NULL value is validated.
    """
    return f"{column} IS NULL OR ({condition})"