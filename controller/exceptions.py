"""Custom exceptions for controller operations."""


class ChallengeNotFound(Exception):
    """Raised when a Deployment for a team/challenge is not found."""
    pass


class MaxInstancesExceeded(Exception):
    """Raised when a team tries to activate more than 4 concurrent instances."""
    pass


class ControllerKeyInvalid(Exception):
    """Raised when X-Controller-Key header is missing or invalid."""
    pass
