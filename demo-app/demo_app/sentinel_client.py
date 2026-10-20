"""Client that reports unhandled demo-app exceptions to Sentinel over HTTP.

Configuration (environment variables):

- ``SENTINEL_URL``: base URL of the Sentinel API (e.g. ``http://localhost:8000``).
- ``SENTINEL_INGEST_KEY``: the application's ingest key (``X-Sentinel-Key``).

Reporting is disabled when either is missing. Reporting never raises: any
failure (connection error, timeout, non-2xx response) is logged and swallowed
so the application's own response is never blocked or altered.
"""

from __future__ import annotations

import logging
import os
import traceback
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

APPLICATION = "demo-app"
INGEST_PATH = "/api/v1/ingest/events"
CORRELATION_HEADER = "X-Request-ID"
DEFAULT_TIMEOUT_SECONDS = 2.0

# Keep payloads within Sentinel's ingest limits.
MAX_MESSAGE_LENGTH = 4096
MAX_STACK_TRACE_LENGTH = 60 * 1024
MAX_CORRELATION_ID_LENGTH = 200


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


class SentinelReporter:
    def __init__(
        self,
        url: str | None,
        ingest_key: str | None,
        environment: str = "development",
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.url = (url or "").strip().rstrip("/") or None
        self.ingest_key = (ingest_key or "").strip() or None
        self.environment = environment
        self.transport = transport
        self.timeout = timeout

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        environment: str = "development",
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> SentinelReporter:
        env = os.environ if environ is None else environ
        return cls(
            env.get("SENTINEL_URL"),
            env.get("SENTINEL_INGEST_KEY"),
            environment,
            transport=transport,
            timeout=timeout,
        )

    @property
    def enabled(self) -> bool:
        return self.url is not None and self.ingest_key is not None

    @property
    def endpoint(self) -> str:
        return f"{self.url}{INGEST_PATH}"

    def build_payload(
        self,
        exc: BaseException,
        *,
        method: str,
        path: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Build the ingest payload. ``path`` must not include the query string."""
        stack_trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        correlation = (correlation_id or "").strip() or str(uuid.uuid4())
        return {
            "application": APPLICATION,
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exception_type": type(exc).__name__,
            "message": _truncate(str(exc), MAX_MESSAGE_LENGTH),
            "stack_trace": _truncate(stack_trace, MAX_STACK_TRACE_LENGTH),
            "environment": self.environment,
            "correlation_id": _truncate(correlation, MAX_CORRELATION_ID_LENGTH),
            "metadata": {"method": method, "path": path.split("?", 1)[0]},
        }

    def send(self, payload: dict[str, Any]) -> bool:
        """POST ``payload`` to Sentinel. Returns True on success; never raises."""
        if not self.enabled:
            return False
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                response = client.post(
                    self.endpoint,
                    json=payload,
                    headers={"X-Sentinel-Key": self.ingest_key or ""},
                )
            if not response.is_success:
                logger.warning("Sentinel rejected the error report: HTTP %s", response.status_code)
                return False
            return True
        except Exception as error:  # noqa: BLE001 - reporting must never break the app
            logger.warning("Could not report the error to Sentinel: %s", type(error).__name__)
            return False

    def report_exception(
        self,
        exc: BaseException,
        *,
        method: str,
        path: str,
        correlation_id: str | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        try:
            payload = self.build_payload(
                exc, method=method, path=path, correlation_id=correlation_id
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not build the Sentinel error report", exc_info=True)
            return False
        return self.send(payload)
