"""PostgreSQL persistence adapter.

The one place in the codebase that knows SQLAlchemy exists. Everything else
depends on ports (see :mod:`app.ports`) and never on this package.

Importing the model classes here is deliberate rather than incidental: Alembic's
autogenerate compares live schema against ``Base.metadata``, and a model that
was never imported is a table it will propose to drop. Importing them in the
package's ``__init__`` makes "imported the package" and "metadata is complete"
the same event.
"""

from __future__ import annotations

from app.adapters.persistence.base import (
    NAMING_CONVENTION,
    Base,
    IngestedAtMixin,
    UuidPrimaryKeyMixin,
)
from app.adapters.persistence.database import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    UnsupportedDatabaseError,
    check_connection,
    create_engine_for_url,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    redact_database_url,
    session_scope,
)
from app.adapters.persistence.models import (
    MONEY_PRECISION,
    MONEY_SCALE,
    QUANTITY_PRECISION,
    QUANTITY_SCALE,
    Account,
    Contract,
    ContractPriceTerm,
)

__all__ = [
    "MONEY_PRECISION",
    "MONEY_SCALE",
    "NAMING_CONVENTION",
    "QUANTITY_PRECISION",
    "QUANTITY_SCALE",
    "Account",
    "Base",
    "Contract",
    "ContractPriceTerm",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "IngestedAtMixin",
    "UnsupportedDatabaseError",
    "UuidPrimaryKeyMixin",
    "check_connection",
    "create_engine_for_url",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "redact_database_url",
    "session_scope",
]
