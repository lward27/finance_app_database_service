"""Identity API bodies contain neither provider tokens nor client-selected owners."""

from datetime import datetime
from typing import List, Literal, Optional

from finance_app_database_service.contracts import Contract, GuestView


class IdentityView(GuestView):
    kind: Literal["guest", "registered"]


class EmptyIdentityCommand(Contract):
    pass


class AuthenticationCapabilities(Contract):
    google_enabled: bool
    sign_in_path: str = "/v1/oidc-authentications"
    guest_claim_path: str = "/v1/guest-claims"


class AuthenticationStart(Contract):
    authorization_url: str
    expires_at: datetime


class AuthenticationView(Contract):
    status: Literal["none", "pending", "exchanging", "verified", "completed", "failed"]
    intent: Optional[Literal["sign_in", "guest_claim", "reauthenticate"]] = None
    expires_at: Optional[datetime] = None
    csrf_token: Optional[str] = None
    guest_game_count: int = 0
    code: Optional[str] = None


class AuthenticationComplete(Contract):
    identity: IdentityView
    claimed_games: int
    claim_id: Optional[str] = None


class SessionView(Contract):
    id: str
    created_at: datetime
    expires_at: datetime
    authenticated_at: Optional[datetime]
    is_current: bool


class SessionPage(Contract):
    items: List[SessionView]
    next_cursor: Optional[str]


class SessionRevocations(Contract):
    scope: Literal["other", "all"]


class RevocationView(Contract):
    revoked_count: int
