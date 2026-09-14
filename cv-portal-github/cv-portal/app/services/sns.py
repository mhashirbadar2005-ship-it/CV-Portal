"""SNS notifications for the recruiting inbox.

SNS email is deliberately simple: plain text only, no HTML, no attachments, and
the "From" address is always AWS's. That is fine for an internal alert. If you
ever need branded email or the CV attached, switch this module to SES.
"""

from __future__ import annotations

import logging
import unicodedata

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings

log = logging.getLogger(__name__)

_config = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=10)

_client = None


def client():
    """The cached SNS client, created on first use (see app/services/s3.py)."""
    global _client
    if _client is None:
        kwargs = {"config": _config}
        if settings.aws_region:
            kwargs["region_name"] = settings.aws_region
        _client = boto3.client("sns", **kwargs)
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests between mocked AWS sessions."""
    global _client
    _client = None

SUBJECT_LIMIT = 100


class NotifyError(Exception):
    """SNS refused to publish."""


def _subject(name: str) -> str:
    """ASCII, single line, 100 characters — or SNS rejects the publish call.

    Accented letters are transliterated rather than dropped, so "Hashír" comes
    through as "Hashir" instead of "Hashr".
    """
    raw = f"New CV: {name}"
    folded = unicodedata.normalize("NFKD", raw)
    ascii_only = folded.encode("ascii", "ignore").decode()
    return " ".join(ascii_only.split())[:SUBJECT_LIMIT] or "New CV submission"


def publish_submission(
    *,
    reference: str,
    form,
    s3_key: str,
    download_url: str,
    file_name: str,
    file_size: int,
) -> None:
    lines = [
        f"Reference:   {reference}",
        f"Name:        {form.full_name}",
        f"Email:       {form.email}",
        f"Phone:       {form.phone}",
        f"CNIC:        {form.cnic}",
    ]
    if form.portfolio_url:
        lines.append(f"Portfolio:   {form.portfolio_url}")
    if form.note:
        lines += ["", "Their note:", form.note]
    lines += [
        "",
        f"File:        {file_name} ({file_size / 1024:.0f} KB)",
        f"S3 object:   s3://{settings.s3_bucket}/{s3_key}",
    ]
    if download_url:
        lines += ["", "Temporary download link:", download_url]

    try:
        client().publish(
            TopicArn=settings.sns_topic_arn,
            Subject=_subject(form.full_name),
            Message="\n".join(lines),
        )
    except (ClientError, BotoCoreError) as exc:
        log.exception("sns publish failed for ref=%s", reference)
        raise NotifyError(str(exc)) from exc
