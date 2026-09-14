"""S3 storage for CV files.

The boto3 client is created once and then cached in a module-level global. On
Lambda that global survives between warm invocations, so the TCP connection and
credential lookup are paid for once rather than on every request.

It is built lazily rather than at import time for one practical reason: mocking
libraries such as moto patch botocore when they start, and a client constructed
before that patching is never intercepted. Lazy creation keeps the code
testable without a real AWS account.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.files import CONTENT_TYPES

log = logging.getLogger(__name__)

_config = Config(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=15,
    signature_version="s3v4",
)

_client = None


def client():
    """The cached S3 client, created on first use."""
    global _client
    if _client is None:
        kwargs = {"config": _config}
        if settings.aws_region:
            kwargs["region_name"] = settings.aws_region
        _client = boto3.client("s3", **kwargs)
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests between mocked AWS sessions."""
    global _client
    _client = None


class StorageError(Exception):
    """S3 refused the write."""


def build_key(reference: str, safe_name: str) -> str:
    """submissions/2026/09/06/AV-20260906-K7P2Q4-hashir-cv.pdf"""
    now = datetime.now(timezone.utc)
    prefix = settings.s3_prefix.strip("/")
    return f"{prefix}/{now:%Y/%m/%d}/{reference}-{safe_name}"


def _ascii_meta(value: str) -> str:
    """S3 object metadata must be US-ASCII, so percent-encode anything else."""
    return quote(value, safe="@.-_ ")


def upload_cv(data: bytes, key: str, ext: str, meta: dict[str, str]) -> None:
    try:
        client().put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=data,
            ContentType=CONTENT_TYPES.get(ext, "application/octet-stream"),
            ServerSideEncryption="AES256",
            Metadata={k: _ascii_meta(v) for k, v in meta.items()},
        )
    except (ClientError, BotoCoreError) as exc:
        log.exception("s3 put_object failed for key=%s", key)
        raise StorageError(str(exc)) from exc


def presigned_download_url(key: str, ttl: int | None = None) -> str:
    """A temporary link to the stored CV.

    Note: signed with whatever credentials the process holds. Under Lambda those
    are short-lived STS credentials, so the link dies with them even if `ttl` is
    longer. Treat it as a convenience, not an archive link.
    """
    try:
        return client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=ttl or settings.presigned_url_ttl,
        )
    except (ClientError, BotoCoreError):
        log.exception("could not presign key=%s", key)
        return ""
