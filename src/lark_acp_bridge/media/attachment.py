"""Download, cache, and policy-filter inbound Feishu message attachments.

Mirrors the TypeScript project's ``src/media/cache.ts`` + ``src/media/attachment.ts``:
images and files are downloaded from Feishu, stored as content-addressed files
under ``~/.lark-acp-bridge/media/``, and filtered through size/count/MIME policies.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import structlog

logger = structlog.get_logger()

# --------------------------------------------------------------------------- #
# Policy constants
# --------------------------------------------------------------------------- #

MAX_ATTACHMENT_COUNT = 10
MAX_IMAGE_BYTES = 25 * 1024 * 1024     # 25 MB per image
MAX_FILE_BYTES = 25 * 1024 * 1024      # 25 MB per file
MAX_TOTAL_BYTES = 100 * 1024 * 1024    # 100 MB total

ACCEPTED_IMAGE_MIMES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/gif",
})
ACCEPTED_FILE_MIMES = frozenset({
    "application/pdf", "application/zip",
    "text/plain", "text/markdown", "application/json",
    "text/csv", "text/x-python",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
})

_HASH_PREFIX_LEN = 16  # SHA-256 hex chars used as filename prefix

# MIME → extension fallback when mimetypes.guess_extension returns None
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/json": ".json",
    "text/csv": ".csv",
    "text/x-python": ".py",
}


# --------------------------------------------------------------------------- #
# Data class
# --------------------------------------------------------------------------- #

@dataclass
class AttachmentInfo:
    """Metadata for a downloaded inbound attachment."""

    abs_path: str
    kind: str                          # "image" | "file"
    mime_type: str
    size: int                          # bytes
    file_hash: str                     # SHA-256 hex (truncated)
    original_name: str = ""
    decision: str = "accepted"         # "accepted" | "rejected" | "skipped"
    rejection_reason: str = ""


# --------------------------------------------------------------------------- #
# Download + content-addressed cache
# --------------------------------------------------------------------------- #

def _guess_mime(file_name: str, fallback: str = "application/octet-stream") -> str:
    """Infer MIME type from a file name."""
    mime, _ = mimetypes.guess_type(file_name)
    return mime or fallback


def _ext_for_mime(mime: str) -> str:
    """Return a file extension (with leading dot) for the given MIME type."""
    ext = _MIME_TO_EXT.get(mime)
    if ext:
        return ext
    guessed = mimetypes.guess_extension(mime, strict=False)
    return guessed or ".bin"


async def download_and_cache(
    lark_client: Any,
    message_id: str,
    resource_type: str,
    file_key: str,
    file_name: str,
    cache_dir: str,
    loop: asyncio.AbstractEventLoop,
) -> tuple[AttachmentInfo | None, str | None]:
    """Download a Feishu message resource and store it under a content-addressed path.

    Returns ``(AttachmentInfo, None)`` on success, or ``(None, reason)`` on
    failure where ``reason`` is a short human-readable string describing
    what went wrong (e.g. ``"api error 99991663: invalid access token"`` or
    ``"exception: ConnectionError: ..."``).  Files that already exist in
    the cache (same SHA-256) are reused without re-downloading.
    """
    try:
        raw_bytes, reason = await _fetch_resource(
            lark_client, message_id, resource_type, file_key, loop
        )
    except Exception as exc:
        logger.error(
            "attachment-download-error",
            file_key=file_key,
            resource_type=resource_type,
            error=str(exc),
            exc_info=True,
        )
        return None, f"exception: {type(exc).__name__}: {exc}"

    if raw_bytes is None:
        return None, reason

    # Content-addressed storage
    digest = hashlib.sha256(raw_bytes).hexdigest()[:_HASH_PREFIX_LEN]
    mime = _guess_mime(file_name)
    ext = _ext_for_mime(mime)
    kind = "image" if resource_type == "image" else "file"
    target = Path(cache_dir) / f"{digest}{ext}"

    if not target.exists():
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            await loop.run_in_executor(None, tmp.write_bytes, raw_bytes)
            tmp.rename(target)
        except Exception as exc:
            logger.warning("attachment-write-error", path=str(target), error=str(exc))
            tmp.unlink(missing_ok=True)
            return None

    info = AttachmentInfo(
        abs_path=str(target),
        kind=kind,
        mime_type=mime,
        size=len(raw_bytes),
        file_hash=digest,
        original_name=file_name if kind == "file" else "",
    )
    logger.info(
        "attachment-downloaded",
        kind=kind,
        mime=mime,
        size=len(raw_bytes),
        path=str(target),
    )
    return info, None


async def _fetch_resource(
    lark_client: Any,
    message_id: str,
    resource_type: str,
    file_key: str,
    loop: asyncio.AbstractEventLoop,
) -> tuple[bytes | None, str | None]:
    """Call the Feishu API to download raw bytes for an image or file resource.

    Returns ``(bytes, None)`` on success or ``(None, reason)`` on failure.

    Note: ``im/v1/images/{image_key}`` (i.e. ``client.im.v1.image.get``) only
    works for images **the bot itself uploaded** — using it on a user's
    inbound image_key returns ``234001 Invalid request param``.  To fetch
    user-sent images we must go through the message-resource endpoint with
    ``type="image"`` instead.  Both images and files use the same endpoint;
    only the ``type`` parameter differs.
    """
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    req = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type("image" if resource_type == "image" else "file")
        .build()
    )
    resp = await loop.run_in_executor(
        None, lark_client.im.v1.message_resource.get, req
    )

    if not resp.success():
        logger.warning(
            "attachment-fetch-failed",
            code=resp.code,
            msg=resp.msg,
            file_key=file_key,
            resource_type=resource_type,
        )
        return None, f"api code={resp.code} msg={resp.msg}"

    file_obj: IO[Any] | None = getattr(resp, "file", None)
    if file_obj is None:
        logger.warning("attachment-fetch-empty", file_key=file_key)
        return None, "empty response (no file payload)"

    data = file_obj.read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data, None


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #

def apply_attachment_policy(
    candidates: list[AttachmentInfo],
) -> list[AttachmentInfo]:
    """Enforce count / size / MIME policies and mark each candidate.

    Mutates each ``AttachmentInfo`` in-place (setting ``decision`` and
    ``rejection_reason``).  Returns the same list for convenience.
    Rejected/skipped files are deleted from disk.
    """
    total_bytes = 0
    accepted_count = 0

    for att in candidates:
        # Count check
        if accepted_count >= MAX_ATTACHMENT_COUNT:
            att.decision = "rejected"
            att.rejection_reason = "too-many-attachments"
            _cleanup_rejected(att)
            continue

        # MIME check
        if att.kind == "image" and att.mime_type not in ACCEPTED_IMAGE_MIMES:
            att.decision = "rejected"
            att.rejection_reason = "unsupported-image-mime"
            _cleanup_rejected(att)
            continue
        if att.kind == "file" and att.mime_type not in ACCEPTED_FILE_MIMES:
            att.decision = "skipped"
            att.rejection_reason = "unsupported-file-mime"
            _cleanup_rejected(att)
            continue

        # Per-file size check
        limit = MAX_IMAGE_BYTES if att.kind == "image" else MAX_FILE_BYTES
        if att.size > limit:
            att.decision = "rejected"
            att.rejection_reason = "image-too-large" if att.kind == "image" else "file-too-large"
            _cleanup_rejected(att)
            continue

        # Cumulative total check
        if total_bytes + att.size > MAX_TOTAL_BYTES:
            att.decision = "rejected"
            att.rejection_reason = "run-too-large"
            _cleanup_rejected(att)
            continue

        att.decision = "accepted"
        total_bytes += att.size
        accepted_count += 1

    return candidates


def _cleanup_rejected(att: AttachmentInfo) -> None:
    """Delete a rejected/skipped file from disk."""
    try:
        p = Path(att.abs_path)
        if p.exists():
            p.unlink()
    except OSError:
        pass
