"""The burrow daemon.

A long-running asyncio process that owns live state: the database, the bus, the
running harness processes, their A2A servers, and the file watchers. The CLI is
a thin client over its control socket.

Why a daemon rather than a per-command process? Because lanes are long-lived.
A harness mid-task cannot survive its parent CLI exiting, and two lanes cannot
message each other if there is no process holding both. The daemon is what makes
asynchronous agent-to-agent collaboration possible at all.
"""

from openburrow.daemon.bus import BusHealth, EventBus, Subscription
from openburrow.daemon.filewatch import FileChange, WatcherPool, WorktreeWatcher
from openburrow.daemon.ipc import IpcClient, IpcServer, socket_is_live
from openburrow.daemon.server import Daemon, run_daemon
from openburrow.daemon.sessions import RunningLane, SessionManager

__all__ = [
    "BusHealth",
    "Daemon",
    "EventBus",
    "FileChange",
    "IpcClient",
    "IpcServer",
    "RunningLane",
    "SessionManager",
    "Subscription",
    "WatcherPool",
    "WorktreeWatcher",
    "run_daemon",
    "socket_is_live",
]
