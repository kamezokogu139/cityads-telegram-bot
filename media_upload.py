"""Upload local files to S3-compatible storage for public URLs (Seedance input)."""

from __future__ import annotations

import asyncio
import mimetypes
import uuid
from pathlib import Path

from config import (
    S3_ACCESS_KEY,
    S3_BUCKET,
    S3_ENDPOINT,
    S3_PUBLIC_BASE_URL,
    S3_SECRET_KEY,
    media_upload_configured,
)


class MediaUploadError(RuntimeError):
    pass


def _guess_content_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


async def upload_file(path: str | Path) -> str:
    """Upload a local file and return a publicly reachable URL."""
    if not media_upload_configured():
        raise MediaUploadError(
            "Media upload is not configured. Set S3_BUCKET, S3_ACCESS_KEY, "
            "S3_SECRET_KEY and S3_PUBLIC_BASE_URL in .env (required for image/reference modes)."
        )

    path = Path(path)
    if not path.is_file():
        raise MediaUploadError(f"File not found: {path}")

    key = f"uploads/{uuid.uuid4().hex}{path.suffix.lower()}"
    content_type = _guess_content_type(path)

    def _upload() -> str:
        import boto3
        from botocore.config import Config

        client_kwargs: dict = {
            "aws_access_key_id": S3_ACCESS_KEY,
            "aws_secret_access_key": S3_SECRET_KEY,
            "config": Config(signature_version="s3v4"),
        }
        if S3_ENDPOINT:
            client_kwargs["endpoint_url"] = S3_ENDPOINT

        client = boto3.client("s3", **client_kwargs)
        with path.open("rb") as f:
            client.upload_fileobj(
                f,
                S3_BUCKET,
                key,
                ExtraArgs={"ContentType": content_type},
            )

        base = S3_PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/{key}"

    return await asyncio.to_thread(_upload)
