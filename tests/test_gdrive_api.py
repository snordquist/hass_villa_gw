"""Unit tests for gdrive_upload.api — pure functions, mocked HTTP."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "gdrive_upload"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Synthetic package to satisfy `from .const` relative imports.
pkg = types.ModuleType("gdrive_upload_test_pkg")
pkg.__path__ = [str(PKG)]
sys.modules["gdrive_upload_test_pkg"] = pkg

_load_module("gdrive_upload_test_pkg.const", PKG / "const.py")
api = _load_module("gdrive_upload_test_pkg.api", PKG / "api.py")


class FakeResponse:
    def __init__(self, status: int, json_data: dict | None = None, text: str = ""):
        self.status = status
        self._json = json_data or {}
        self._text = text

    async def json(self) -> dict:
        return self._json

    async def text(self) -> str:
        return self._text


@pytest.mark.asyncio
async def test_ensure_folder_creates_when_missing():
    """ensure_folder creates a single-level folder if it doesn't exist."""
    request = AsyncMock()
    # 1st call: search → empty
    # 2nd call: create → returns id
    request.side_effect = [
        FakeResponse(200, {"files": []}),
        FakeResponse(200, {"id": "FOLDER_ID_X"}),
    ]
    wrapper = api.DriveApi(request)
    folder_id = await wrapper.ensure_folder("MyFolder")
    assert folder_id == "FOLDER_ID_X"
    assert request.call_count == 2
    # 2nd call must be a POST to files endpoint with mimeType=folder
    create_call = request.call_args_list[1]
    assert create_call.args[0] == "POST"
    assert "drive/v3/files" in create_call.args[1]
    assert create_call.kwargs["json"]["mimeType"] == "application/vnd.google-apps.folder"


@pytest.mark.asyncio
async def test_ensure_folder_nested_uses_cache_on_second_call():
    """ensure_folder caches the resolved path — 2nd call makes 0 requests."""
    request = AsyncMock()
    request.side_effect = [
        # Resolve "A" under root: search → exists
        FakeResponse(200, {"files": [{"id": "A_ID", "name": "A"}]}),
        # Resolve "B" under A: search → missing
        FakeResponse(200, {"files": []}),
        # Create "B" under A
        FakeResponse(200, {"id": "B_ID"}),
    ]
    wrapper = api.DriveApi(request)
    folder_id = await wrapper.ensure_folder("A/B")
    assert folder_id == "B_ID"
    assert request.call_count == 3

    # 2nd call hits cache — no new requests
    folder_id2 = await wrapper.ensure_folder("A/B")
    assert folder_id2 == "B_ID"
    assert request.call_count == 3


@pytest.mark.asyncio
async def test_upload_two_phase_resumable(tmp_path):
    """upload() initiates resumable upload, then sends body, returns file id."""
    payload = b"\x00\x01\x02fake-mp4-content"
    src = tmp_path / "clip.mp4"
    src.write_bytes(payload)

    request = AsyncMock()
    # Phase 1: POST → 200 with Location header
    init_resp = FakeResponse(200, {})
    init_resp.headers = {"Location": "https://upload.example/session/abc"}
    # Phase 2: PUT session URL → returns file metadata
    final_resp = FakeResponse(200, {"id": "FILE_ID", "webViewLink": "https://drive/view/X"})
    request.side_effect = [init_resp, final_resp]

    wrapper = api.DriveApi(request)
    result = await wrapper.upload(str(src), folder_id="PARENT_ID", filename="clip.mp4")
    assert result["id"] == "FILE_ID"
    assert result["webViewLink"] == "https://drive/view/X"
    # phase 1 = POST to upload endpoint with metadata
    assert request.call_args_list[0].args[0] == "POST"
    assert "uploadType=resumable" in request.call_args_list[0].args[1]
    # phase 2 = PUT to session URL with the bytes
    assert request.call_args_list[1].args[0] == "PUT"
    assert request.call_args_list[1].args[1] == "https://upload.example/session/abc"
    assert request.call_args_list[1].kwargs["data"] == payload


@pytest.mark.asyncio
async def test_make_shareable_returns_share_url():
    request = AsyncMock()
    request.return_value = FakeResponse(200, {"id": "perm-id"})
    wrapper = api.DriveApi(request)
    url = await wrapper.make_shareable("FILE_ID")
    # Anyone-with-link share URL format
    assert url == "https://drive.google.com/file/d/FILE_ID/view"
    # exactly one POST to .../FILE_ID/permissions
    assert request.call_args.args[0] == "POST"
    assert "FILE_ID/permissions" in request.call_args.args[1]
    assert request.call_args.kwargs["json"] == {"type": "anyone", "role": "reader"}


@pytest.mark.asyncio
async def test_ensure_folder_raises_on_5xx():
    request = AsyncMock(return_value=FakeResponse(500, text="Internal Server Error"))
    wrapper = api.DriveApi(request)
    with pytest.raises(api.DriveApiError) as exc:
        await wrapper.ensure_folder("Anything")
    assert "500" in str(exc.value)
