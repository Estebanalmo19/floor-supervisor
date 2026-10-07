"""Application exception hierarchy.

Exception messages must never contain secrets (tokens, passwords) or full card values.
"""


class FloorSupervisorError(Exception):
    """Base class for all application errors."""


class ConfigError(FloorSupervisorError):
    """Configuration is missing or invalid."""


# --- Card Resolver -----------------------------------------------------------


class CardResolverError(FloorSupervisorError):
    """Base class for Card Resolver failures."""


class CardResolverUnavailableError(CardResolverError):
    """Timeout, connection failure, rate limiting or server-side (5xx) error."""


class CardNotResolvedError(CardResolverError):
    """Card Resolver answered but could not resolve the card to an employee."""


class CardResolverResponseError(CardResolverError):
    """Unexpected status (e.g. 401/403) or a malformed response body."""


# --- Persistence -------------------------------------------------------------


class RepositoryError(FloorSupervisorError):
    """A database operation failed (connection, timeout, SQL error)."""


class EmployeeDataIntegrityError(FloorSupervisorError):
    """The HiBob lookup returned more than one row for a single employee ID."""
