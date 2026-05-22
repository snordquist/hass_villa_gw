"""Drive REST wrapper — pure, takes an injected async request callable."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .const import (
    DRIVE_API_FILES,
    DRIVE_API_UPLOAD,
    DRIVE_API_PERMISSIONS,
    FOLDER_MIME,
)

_LOGGER = logging.getLogger(__name__)

# RequestFn signature: request(method, url, *, json=None, params=None,
#                              data=None, headers=None) -> Response
RequestFn = Callable[..., Awaitable[Any]]


class DriveApiError(Exception):
    """Drive API returned a non-2xx response."""


class DriveApi:
    """Thin wrapper around Google Drive v3 REST."""

    def __init__(self, request: RequestFn) -> None:
        self._request = request
        self._folder_cache: dict[str, str] = {}  # path → folder_id

    async def _search_folder(self, name: str, parent_id: str | None) -> str | None:
        """Return folder id for name under parent_id, or None."""
        q_parts = [
            f"mimeType = '{FOLDER_MIME}'",
            f"name = '{name}'",
            "trashed = false",
        ]
        if parent_id:
            q_parts.append(f"'{parent_id}' in parents")
        params = {"q": " and ".join(q_parts), "fields": "files(id,name)"}
        resp = await self._request("GET", DRIVE_API_FILES, params=params)
        if resp.status != 200:
            raise DriveApiError(f"search_folder {name}: {resp.status} {await resp.text()}")
        data = await resp.json()
        files = data.get("files", [])
        return files[0]["id"] if files else None

    async def _create_folder(self, name: str, parent_id: str | None) -> str:
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        resp = await self._request("POST", DRIVE_API_FILES, json=body)
        if resp.status not in (200, 201):
            raise DriveApiError(f"create_folder {name}: {resp.status} {await resp.text()}")
        return (await resp.json())["id"]

    async def ensure_folder(self, path: str) -> str:
        """Resolve (and create if needed) a slash-delimited folder path."""
        if path in self._folder_cache:
            return self._folder_cache[path]
        parent_id: str | None = None
        for part in (p for p in path.split("/") if p):
            existing = await self._search_folder(part, parent_id)
            parent_id = existing or await self._create_folder(part, parent_id)
        assert parent_id is not None, f"empty folder path: {path!r}"
        self._folder_cache[path] = parent_id
        return parent_id

    async def upload(self, file_path: str, folder_id: str, filename: str) -> dict:
        """Two-phase resumable upload. Returns Drive file metadata dict."""
        # Phase 1: initiate session
        metadata = {"name": filename, "parents": [folder_id]}
        resp = await self._request(
            "POST",
            f"{DRIVE_API_UPLOAD}?uploadType=resumable",
            json=metadata,
        )
        if resp.status not in (200, 201):
            raise DriveApiError(f"upload init: {resp.status} {await resp.text()}")
        session_url = resp.headers.get("Location")
        if not session_url:
            raise DriveApiError("upload init: no Location header")

        # Phase 2: send bytes (single chunk — files are small, ~10 MB)
        with open(file_path, "rb") as fh:
            payload = fh.read()
        put_resp = await self._request(
            "PUT",
            session_url,
            data=payload,
            headers={"Content-Type": "video/mp4"},
        )
        if put_resp.status not in (200, 201):
            raise DriveApiError(f"upload PUT: {put_resp.status} {await put_resp.text()}")
        return await put_resp.json()

    async def make_shareable(self, file_id: str) -> str:
        """Add anyone-with-link reader permission, return shareable URL."""
        url = DRIVE_API_PERMISSIONS.format(file_id=file_id)
        resp = await self._request(
            "POST",
            url,
            json={"type": "anyone", "role": "reader"},
        )
        if resp.status not in (200, 201):
            raise DriveApiError(f"make_shareable: {resp.status} {await resp.text()}")
        return f"https://drive.google.com/file/d/{file_id}/view"
