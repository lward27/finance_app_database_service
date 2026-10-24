"""Remove callback credentials before routing, URL generation, and Uvicorn access logs."""

import logging
from urllib.parse import parse_qsl, unquote

CALLBACK_PATH = "/v1/oidc/callback"


class CallbackAccessLogFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's proxy middleware can copy the scope before our application sees it.
        if isinstance(record.args, tuple) and len(record.args) == 5 and isinstance(record.args[2], str):
            path = record.args[2]
            if unquote(path.partition("?")[0]).rstrip("/").endswith(CALLBACK_PATH):
                record.args = (*record.args[:2], path.partition("?")[0], *record.args[3:])
        return True


def install_callback_log_filter():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, CallbackAccessLogFilter) for item in logger.filters):
        logger.addFilter(CallbackAccessLogFilter())


class CallbackCredentialsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == CALLBACK_PATH:
            raw = scope.get("query_string", b"")
            scope["query_string"] = b""
            try:
                if len(raw) > 16 * 1024:
                    raise ValueError()
                pairs = parse_qsl(raw.decode("ascii"), strict_parsing=True,
                                  encoding="utf-8", errors="strict", max_num_fields=16)
                values = dict(pairs)
                if len(values) != len(pairs):
                    raise ValueError()
                scope.setdefault("state", {})["oidc_callback"] = values
            except (ValueError, UnicodeError):
                scope.setdefault("state", {})["oidc_callback"] = None
        await self.app(scope, receive, send)
