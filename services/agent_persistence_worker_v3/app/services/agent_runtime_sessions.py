"""Agent Runtime session lifecycle client for the persistence worker."""

from __future__ import annotations

import asyncio
from typing import Any

import google.auth
import httpx
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

from common.schemas import ThreadDeleteRequestedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.core.errors import RetryableWorkerError


AUTH_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


class AgentRuntimeSessionNotFoundError(Exception):
    """Signals that the runtime session has already been removed."""


class AgentRuntimeSessionsClient:
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.runtime_delete_timeout_seconds,
                write=settings.runtime_delete_timeout_seconds,
                pool=10.0,
            )
        )
        self._credentials: Credentials | None = None
        self._auth_lock = asyncio.Lock()

    async def delete_session(self, event: ThreadDeleteRequestedEvent) -> None:
        if not event.agent_resource_name:
            raise RetryableWorkerError(
                f"Missing Agent Runtime resource name for delete event {event.event_id}."
            )
        headers = await self._authorized_headers()
        url = f"https://aiplatform.googleapis.com/v1beta1/{event.agent_resource_name}/sessions/{event.session_id}"

        try:
            response = await self._http_client.delete(url, headers=headers)
        except httpx.RequestError as exc:
            raise RetryableWorkerError(
                f"Failed to reach Agent Runtime for session delete {event.session_id}: {exc}"
            ) from exc

        if response.status_code in {200, 204}:
            return
        if response.status_code == 404:
            raise AgentRuntimeSessionNotFoundError(event.session_id)
        if response.status_code >= 500:
            raise RetryableWorkerError(
                f"Agent Runtime session delete failed with {response.status_code}: {response.text}"
            )
        raise RetryableWorkerError(
            f"Agent Runtime session delete returned unexpected status {response.status_code}: {response.text}"
        )

    async def close(self) -> None:
        await self._http_client.aclose()

    async def _authorized_headers(self) -> dict[str, str]:
        token = await self._access_token()
        return {"Authorization": f"Bearer {token}"}

    async def _access_token(self) -> str:
        async with self._auth_lock:
            if self._credentials is None:
                self._credentials = await asyncio.to_thread(self._default_credentials)
            if not self._credentials.valid or not self._credentials.token:
                await asyncio.to_thread(self._credentials.refresh, GoogleAuthRequest())
            token = self._credentials.token
            if not token:
                raise RetryableWorkerError(
                    "Unable to obtain a Google access token for Agent Runtime session deletion."
                )
            return token

    def _default_credentials(self) -> Credentials:
        credentials, _ = google.auth.default(scopes=AUTH_SCOPES)
        return credentials


_client_singleton: AgentRuntimeSessionsClient | None = None
_client_lock = asyncio.Lock()


async def get_agent_runtime_sessions_client() -> AgentRuntimeSessionsClient:
    global _client_singleton
    if _client_singleton is None:
        async with _client_lock:
            if _client_singleton is None:
                _client_singleton = AgentRuntimeSessionsClient()
    return _client_singleton


async def close_agent_runtime_sessions_client() -> None:
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.close()
    _client_singleton = None
