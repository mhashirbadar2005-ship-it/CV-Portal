"""CV file handling: read with a hard size cap, then verify it really is a CV."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from fastapi import UploadFile

from app.config import settings

CHUNK = 64 * 1024

# First bytes of the formats we accept. Extension checks alone are trivial to
# fake, so we look at the actual content too.
SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    "docx": (b"PK\x03\x04",),
    "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Unambiguous alphabet: no 0/O or 1/I to misread over the phone.
REF_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class FileRejected(Exception):
    """The upload is missing, too big, or not an accepted document."""


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def safe_filename(filename: str, limit: int = 60) -> str:
    """Strip paths and anything that could confuse S3 keys or email clients."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")
    return (base or "cv")[:limit]


def new_reference() -> str:
    """A short human-quotable ID, e.g. AV-20260906-K7P2Q4."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    tail = "".join(secrets.choice(REF_ALPHABET) for _ in range(6))
    return f"AV-{stamp}-{tail}"


async def read_cv(upload: UploadFile | None) -> tuple[bytes, str, str]:
    """Return (data, extension, safe_name) or raise FileRejected.

    The file is streamed in chunks and abandoned the moment it crosses the size
    limit, so a huge upload never fully lands in memory.
    """
    if upload is None or not upload.filename:
        raise FileRejected("Attach your CV before sending.")

    ext = extension_of(upload.filename)
    allowed = settings.extension_list
    if ext not in allowed:
        raise FileRejected(f"We accept {', '.join(allowed).upper()} files.")

    limit = settings.max_upload_bytes
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise FileRejected(f"That file is over {settings.max_upload_mb} MB. Compress it and try again.")
        parts.append(chunk)

    data = b"".join(parts)
    if total == 0:
        raise FileRejected("That file is empty.")

    expected = SIGNATURES.get(ext)
    if expected and not data.startswith(expected):
        raise FileRejected(f"The contents don't match a {ext.upper()} file.")

    return data, ext, safe_filename(upload.filename)
