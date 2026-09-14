"""Application settings.

Every value below can be overridden with an environment variable of the same
name (case-insensitive). Locally they come from `.env`; on Lambda they come from
the function's environment variables.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Branding / copy -----------------------------------------------------
    company_name: str = "Averon Automation"
    careers_email: str = "careers@averon.example"

    # --- AWS -----------------------------------------------------------------
    # Leave `aws_region` unset on Lambda: the runtime already exports AWS_REGION
    # and boto3 picks it up on its own.
    aws_region: str | None = None
    s3_bucket: str = ""
    s3_prefix: str = "submissions"
    sns_topic_arn: str = ""

    # --- Upload rules --------------------------------------------------------
    # Ceiling is set by Lambda, not by us: a synchronous invoke payload cannot
    # exceed 6 MB, and API Gateway base64-encodes the file first (+~37%).
    # 4 MB is the largest value that reliably fits. Bigger files need the
    # presigned-upload path described in README.md.
    max_upload_mb: int = 4
    allowed_extensions: str = "pdf,doc,docx"

    # Presigned links are signed with the caller's credentials. On Lambda those
    # are temporary STS credentials, so a link never outlives the function's
    # credential lifetime no matter what you ask for here. Keep it modest.
    presigned_url_ttl: int = 3600

    # --- Runtime -------------------------------------------------------------
    environment: str = "local"
    # Set to "/prod" (or whatever your API Gateway stage is) when the app is not
    # served from the root path. Empty for HTTP API $default stages.
    root_path: str = ""
    # When true, the app validates input and renders pages but skips S3 + SNS.
    # Useful for building the UI before any AWS account exists.
    dry_run: bool = False

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def extension_list(self) -> list[str]:
        return [e.strip().lower().lstrip(".") for e in self.allowed_extensions.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
