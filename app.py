from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("provenance_service")

IMAGE_TYPES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (),
}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


API_TOKEN = os.getenv("SERVICE_API_TOKEN", "")
MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_BYTES", 10 * 1024 * 1024, 1, 50 * 1024 * 1024)
REQUEST_TIMEOUT_SECONDS = _env_int("REQUEST_TIMEOUT_SECONDS", 25, 1, 120)
RATE_LIMIT_REQUESTS = _env_int("RATE_LIMIT_REQUESTS", 10, 1, 1000)
RATE_LIMIT_WINDOW_SECONDS = _env_int("RATE_LIMIT_WINDOW_SECONDS", 60, 1, 3600)


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            # Bound memory if a caller can supply many distinct source addresses.
            if len(self._events) >= 10_000:
                stale = [
                    address
                    for address, events in self._events.items()
                    if not events or now - events[-1] >= RATE_LIMIT_WINDOW_SECONDS
                ]
                for address in stale:
                    self._events.pop(address, None)
                if key not in self._events and len(self._events) >= 10_000:
                    return False
            events = self._events[key]
            while events and now - events[0] >= RATE_LIMIT_WINDOW_SECONDS:
                events.popleft()
            if len(events) >= RATE_LIMIT_REQUESTS:
                return False
            events.append(now)
            return True


limiter = SlidingWindowLimiter()


def detect_image_type(content_type: str, data: bytes) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Supported image types: JPEG, PNG, WebP")
    if media_type == "image/webp":
        valid = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    else:
        valid = any(data.startswith(magic) for magic in IMAGE_TYPES[media_type])
    if not valid:
        raise HTTPException(status_code=415, detail="Image bytes do not match the declared media type")
    return media_type


def read_c2pa(media_type: str, data: bytes) -> dict[str, Any]:
    """Validate a C2PA manifest from an in-memory stream; never create an image file."""
    try:
        import c2pa

        with c2pa.Reader(media_type, BytesIO(data)) as reader:
            report = json.loads(reader.json())
    except Exception as exc:
        # SDK errors can reveal data-derived strings; log only the failure class.
        logger.info("c2pa_read_failed error_type=%s", type(exc).__name__)
        if "ManifestNotFound" in type(exc).__name__:
            return {"state": "not_found", "manifest": None, "validation_state": "NotPresent"}
        return {"state": "unavailable", "manifest": None, "validation_state": None}

    return summarize_c2pa_report(report)


def summarize_c2pa_report(report: dict[str, Any]) -> dict[str, Any]:
    """Map the SDK's top-level validation state without confusing it with AI detection."""

    active_id = report.get("active_manifest")
    manifests = report.get("manifests") or {}
    active = manifests.get(active_id) if active_id else None
    validation_state = report.get("validation_state")
    normalized_state = validation_state.lower() if isinstance(validation_state, str) else None
    if active is None or normalized_state == "notpresent":
        state = "not_found"
    elif normalized_state == "trusted":
        state = "verified"
    elif normalized_state in {"valid", "invalid"}:
        state = "unverified_claim"
    else:
        state = "unavailable"
    summary = None
    if isinstance(active, dict):
        signature = active.get("signature_info") or {}
        summary = {
            "title": active.get("title"),
            "format": active.get("format"),
            "claim_generator": active.get("claim_generator"),
            "issuer": signature.get("issuer") if isinstance(signature, dict) else None,
            "validation_state": validation_state,
        }
    return {
        "state": state,
        "manifest": summary,
        "validation_state": validation_state,
    }


async def check_openai(data: bytes, media_type: str, api_key: str) -> dict[str, Any]:
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://api.openai.com/v1/content_provenance_checks",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("image", data, media_type)},
            )
        if response.status_code >= 400:
            logger.info("openai_check_failed status=%d", response.status_code)
            return {"state": "failed", "http_status": response.status_code}
        body = response.json()
        return {"state": "completed", "results": body.get("results", [])}
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("openai_check_failed error_type=%s", type(exc).__name__)
        return {"state": "failed"}


def overall_status(c2pa_result: dict[str, Any], openai_result: dict[str, Any] | None) -> str:
    if c2pa_result["state"] == "verified":
        return "verified_source"
    if openai_result is not None:
        if openai_result["state"] in {"failed", "unavailable"}:
            if c2pa_result["state"] == "unverified_claim":
                return "unverified_claim_found"
            return "detection_failed_or_unsupported"
        results = openai_result.get("results", [])
        if any(item.get("outcome") == "detected" for item in results):
            # OpenAI provenance is a supported signal, but only C2PA trusted/valid
            # manifests are reported as a cryptographically verified source.
            return "unverified_claim_found"
    if c2pa_result["state"] == "unverified_claim":
        return "unverified_claim_found"
    if c2pa_result["state"] == "unavailable":
        return "detection_failed_or_unsupported"
    return "no_supported_signal_found"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not API_TOKEN or len(API_TOKEN) < 32:
        raise RuntimeError("SERVICE_API_TOKEN must be set to at least 32 characters")
    yield


app = FastAPI(title="Content Provenance Verification Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/verify")
async def verify(
    request: Request,
    include_openai: bool = False,
    openai_fallback: bool = False,
) -> JSONResponse:
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {API_TOKEN}"
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

    peer = request.client.host if request.client else "unknown"
    if not await limiter.allow(peer):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    media_type = request.headers.get("content-type", "")
    declared_size = request.headers.get("content-length")
    if declared_size:
        try:
            if int(declared_size) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="Image exceeds the configured size limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image exceeds the configured size limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="Empty request body")
    media_type = detect_image_type(media_type, data)

    c2pa_result = await asyncio.wait_for(
        asyncio.to_thread(read_c2pa, media_type, data), timeout=REQUEST_TIMEOUT_SECONDS
    )
    openai_result = None
    fallback_skipped = (
        openai_fallback
        and not include_openai
        and c2pa_result.get("state") == "verified"
        and isinstance(c2pa_result.get("manifest"), dict)
        and bool(c2pa_result["manifest"].get("issuer"))
    )
    if fallback_skipped:
        openai_result = {"state": "skipped", "reason": "trusted_c2pa_source"}
    elif include_openai or openai_fallback:
        api_key = request.headers.get("x-openai-api-key", "")
        if not api_key:
            openai_result = {"state": "unavailable", "reason": "missing_request_key"}
        elif len(api_key) > 4096 or any(char.isspace() or ord(char) < 33 for char in api_key):
            raise HTTPException(status_code=400, detail="Invalid X-OpenAI-API-Key header")
        else:
            openai_result = await check_openai(data, media_type, api_key)

    response = {
        "status": overall_status(c2pa_result, openai_result),
        "c2pa": c2pa_result,
        "openai_provenance": openai_result,
        "other_providers": {"gemini_synthid": "unsupported_no_official_api_integrated"},
        "limitations": [
            "No supported signal does not prove that content is human-created.",
            "OpenAI provenance checks are not a general-purpose AI detector.",
            "OpenAI provenance checks do not identify images from other providers.",
        ],
    }
    return JSONResponse(response, headers={"Cache-Control": "no-store"})


@app.exception_handler(Exception)
async def safe_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.error("request_failed error_type=%s", type(exc).__name__)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)
