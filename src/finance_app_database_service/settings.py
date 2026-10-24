"""Explicit configuration for the new authenticated API boundary."""

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import Request


@dataclass(frozen=True)
class SecuritySettings:
    allowed_origins: tuple[str, ...] = ()
    service_token: str = field(default="", repr=False)
    guest_rate_limit_key: str = field(default="", repr=False)
    guest_session_seconds: int = 7 * 24 * 60 * 60
    guest_creation_limit: int = 10
    guest_creation_window_seconds: int = 600
    games_enabled: bool = False
    google_auth_enabled: bool = False
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    google_redirect_uri: str = ""
    google_completion_uri: str = ""
    registered_session_seconds: int = 30 * 24 * 60 * 60
    oidc_flow_seconds: int = 600
    recent_auth_seconds: int = 300

    def __post_init__(self):
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query
                or parsed.fragment or "*" in origin
                or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))
            ):
                raise ValueError("Configure exact HTTPS frontend origins (HTTP only on loopback)")
        if min(self.guest_session_seconds, self.guest_creation_limit,
               self.guest_creation_window_seconds, self.registered_session_seconds,
               self.oidc_flow_seconds, self.recent_auth_seconds) <= 0:
            raise ValueError("Session and rate limits must be positive")
        if self.oidc_flow_seconds > 600 or self.recent_auth_seconds > 600:
            raise ValueError("Authentication windows must not exceed ten minutes")
        if self.google_auth_enabled:
            if (not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", self.google_client_id)
                    or len(self.google_client_secret) < 16 or len(self.guest_rate_limit_key) < 32):
                raise ValueError("Google authentication requires client credentials and rate-limit configuration")
            for value in (self.google_redirect_uri, self.google_completion_uri):
                parsed = urlsplit(value)
                if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                        or parsed.query or parsed.fragment or not parsed.path or "*" in value):
                    raise ValueError("Authentication callbacks require fixed HTTPS URLs")
            callback = urlsplit(self.google_redirect_uri)
            if callback.path != "/v1/oidc/callback":
                raise ValueError("Configure the exact API OIDC callback path")
            completed = urlsplit(self.google_completion_uri)
            if f"{completed.scheme}://{completed.netloc}" not in self.allowed_origins:
                raise ValueError("Authentication completion must use an allowed frontend origin")

    @classmethod
    def from_environment(cls):
        return cls(
            allowed_origins=tuple(
                origin.strip() for origin in os.getenv("FINANCE_ALLOWED_ORIGINS", "").split(",")
                if origin.strip()
            ),
            service_token=os.getenv("FINANCE_SERVICE_TOKEN", ""),
            guest_rate_limit_key=os.getenv("FINANCE_GUEST_RATE_LIMIT_KEY", ""),
            games_enabled=os.getenv("FINANCE_GAMES_ENABLED", "").lower() == "true",
            google_auth_enabled=os.getenv("FINANCE_GOOGLE_AUTH_ENABLED", "").lower() == "true",
            google_client_id=os.getenv("FINANCE_GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("FINANCE_GOOGLE_CLIENT_SECRET", ""),
            google_redirect_uri=os.getenv("FINANCE_GOOGLE_REDIRECT_URI", ""),
            google_completion_uri=os.getenv("FINANCE_GOOGLE_COMPLETION_URI", ""),
        )


def get_security_settings(request: Request) -> SecuritySettings:
    return request.app.state.security_settings
