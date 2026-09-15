"""OpenBurrow relay.

The only component that assumes more than one machine. It relays bus events
between daemons and exchanges CRDT updates for the brain doc, and it does both
without understanding either: it never assigns a sequence number, never merges a
document, and never runs a lane.

That restraint is the design. The bus log on the owning daemon is the record of
truth, and a relay that tried to be a second authority would have to answer
questions — "which event came first across two machines?" — that have no correct
answer at this layer.

See ``README.md`` next to this file for what the relay deliberately does not do.
"""

from openburrow.relay.app import create_app
from openburrow.relay.config import RelaySettings
from openburrow.relay.errors import RelayError
from openburrow.relay.hub import Connection, Hub
from openburrow.relay.models import DocSnapshot, Invite, Member, RelayEvent, Room, RoomRole
from openburrow.relay.ratelimit import BucketRegistry, TokenBucket
from openburrow.relay.security import TokenClaims, issue_token, verify_token
from openburrow.relay.state import AppState
from openburrow.relay.store import AppendResult, RelayStore

__all__ = [
    "AppState",
    "AppendResult",
    "BucketRegistry",
    "Connection",
    "DocSnapshot",
    "Hub",
    "Invite",
    "Member",
    "RelayError",
    "RelayEvent",
    "RelaySettings",
    "RelayStore",
    "Room",
    "RoomRole",
    "TokenBucket",
    "TokenClaims",
    "create_app",
    "issue_token",
    "verify_token",
]
