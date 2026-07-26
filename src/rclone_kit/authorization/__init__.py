"""Rclone-owned remote authorization (see `docs/rclone_authorization_design.md`).

Drives rclone's non-interactive `config/create`/`config/update` OAuth state
machine entirely through the existing shared embedded runtime - never a
second native library load, never a spawned `rclone` process. Importing this
package must not start a thread, bind a socket, or touch the network;
resource creation begins only when `AuthorizationManager.start()` is called.
"""

from __future__ import annotations

from rclone_kit.authorization.exceptions import (
    AuthorizationCancelledError,
    AuthorizationError,
    AuthorizationExpiredError,
    AuthorizationQueueFullError,
    AuthorizationRejectedError,
    AuthorizationRelayError,
    AuthorizationRemoteNameConflictError,
    AuthorizationStartError,
    AuthorizationUnsupportedPromptError,
)
from rclone_kit.authorization.manager import AuthorizationManager
from rclone_kit.authorization.session import AuthorizationSession
from rclone_kit.authorization.types import (
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationStatus,
    RelayRequest,
    RelayResponse,
    RemoteConflictPolicy,
    Secret,
)

__all__ = [
    "AuthorizationCancelledError",
    "AuthorizationError",
    "AuthorizationExpiredError",
    "AuthorizationManager",
    "AuthorizationQueueFullError",
    "AuthorizationRejectedError",
    "AuthorizationRelayError",
    "AuthorizationRemoteNameConflictError",
    "AuthorizationRequest",
    "AuthorizationResult",
    "AuthorizationSession",
    "AuthorizationStartError",
    "AuthorizationStatus",
    "AuthorizationUnsupportedPromptError",
    "RelayRequest",
    "RelayResponse",
    "RemoteConflictPolicy",
    "Secret",
]
