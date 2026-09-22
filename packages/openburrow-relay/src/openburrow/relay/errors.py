"""Relay errors.

Every one carries a ``status_code`` alongside the usual OpenBurrow ``code``,
``hint``, and ``context``. Putting the HTTP status on the exception rather than
in a mapping in the app module means adding an error cannot forget to pick a
status — the failure mode of a forgotten mapping entry is a 500 for something
that is plainly a 403, which is a bad bug to ship.

``code`` stays stable and machine-readable; ``status_code`` is transport.
"""

from __future__ import annotations

from typing import Any

from openburrow.core.errors import OpenBurrowError


class RelayError(OpenBurrowError):
    """Base class for relay errors."""

    code = "openburrow.relay_error"
    status_code = 500


# --------------------------------------------------------------------------
# Authentication and authorisation
# --------------------------------------------------------------------------
class AuthError(RelayError):
    """The caller is not who they claim to be."""

    code = "openburrow.relay_auth_failed"
    status_code = 401


class TokenExpiredError(AuthError):
    code = "openburrow.relay_token_expired"


class NotAMemberError(RelayError):
    """Authenticated, but not a member of the room being addressed.

    Distinct from :class:`AuthError` on purpose: ``401`` means "authenticate and
    retry", ``403`` means "do not bother". Collapsing the two produces clients
    that retry forever against a door that will never open.
    """

    code = "openburrow.relay_not_a_member"
    status_code = 403


class InviteInvalidError(RelayError):
    code = "openburrow.relay_invite_invalid"
    status_code = 401


class RoomNotFoundError(RelayError):
    code = "openburrow.relay_room_not_found"
    status_code = 404


class BadRequestError(RelayError):
    """The request is well-formed JSON but semantically unusable.

    400, not the base class's 500. A malformed ``since`` parameter is the
    caller's mistake, and reporting it as a server error sends them looking in
    the wrong place — and pollutes the error rate with things that are not faults.
    """

    code = "openburrow.relay.bad_request"
    status_code = 400


class AdminSurfaceDisabledError(RelayError):
    """Room provisioning is not enabled on this relay.

    503 rather than 403: nothing is forbidden, the capability simply is not
    configured here. The distinction tells an operator whether to look at their
    credentials or their environment.
    """

    code = "openburrow.relay.admin_disabled"
    status_code = 503


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------
class RateLimitedError(RelayError):
    """Too many events for this connection's bucket."""

    code = "openburrow.relay_rate_limited"
    status_code = 429

    def __init__(self, message: str, *, retry_after: float, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after
        self.context.setdefault("retry_after_s", round(retry_after, 3))


class RoomFullError(RelayError):
    """The room is at its connection cap."""

    code = "openburrow.relay_room_full"
    status_code = 503


class FrameTooLargeError(RelayError):
    code = "openburrow.relay_frame_too_large"
    status_code = 413


# --------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------
class StorageUnavailableError(RelayError):
    """Postgres is unreachable or the schema is missing."""

    code = "openburrow.relay_storage_unavailable"
    status_code = 503


__all__ = [
    "AdminSurfaceDisabledError",
    "AuthError",
    "BadRequestError",
    "FrameTooLargeError",
    "InviteInvalidError",
    "NotAMemberError",
    "RateLimitedError",
    "RelayError",
    "RoomFullError",
    "RoomNotFoundError",
    "StorageUnavailableError",
    "TokenExpiredError",
]
