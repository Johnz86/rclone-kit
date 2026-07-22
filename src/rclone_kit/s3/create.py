import logging
import warnings
from dataclasses import dataclass
from typing import Any

from boto3.session import Session
from botocore.client import BaseClient
from botocore.config import Config

from rclone_kit.s3.types import S3Credentials, S3Provider

logger = logging.getLogger(__name__)

_DEFAULT_BACKBLAZE_ENDPOINT = "https://s3.us-west-002.backblazeb2.com"
_MAX_CONNECTIONS = 10
_TIMEOUT_READ = 120
_TIMEOUT_CONNECT = 60


@dataclass
class S3Config:
    max_pool_connections: int | None = None
    timeout_connection: int | None = None
    timeout_read: int | None = None
    verbose: bool | None = None

    def resolve_defaults(self) -> None:
        self.max_pool_connections = self.max_pool_connections or _MAX_CONNECTIONS
        self.timeout_connection = self.timeout_connection or _TIMEOUT_CONNECT
        self.timeout_read = self.timeout_read or _TIMEOUT_READ
        self.verbose = self.verbose or False


def _create_s3_client(
    s3_creds: S3Credentials,
    s3_config: S3Config,
    *,
    default_endpoint: str | None = None,
    normalize_schema_naive_endpoint: bool = False,
    payload_signing_enabled: bool | None = None,
) -> BaseClient:
    """Create and return an S3 client, shared by every provider-specific builder."""
    access_key = s3_creds.access_key_id
    secret_key = s3_creds.secret_access_key
    endpoint_url = s3_creds.endpoint_url
    if default_endpoint is not None:
        endpoint_url = endpoint_url or default_endpoint
    if (
        normalize_schema_naive_endpoint
        and endpoint_url is not None
        and not endpoint_url.startswith("http")
    ):
        if s3_config.verbose:
            warnings.warn(
                f"Endpoint URL is schema naive: {endpoint_url}, assuming HTTPS", stacklevel=2
            )
        endpoint_url = f"https://{endpoint_url}"
    s3_config.resolve_defaults()
    session = Session()
    config_kwargs: dict[str, Any] = {
        "signature_version": "s3v4",
        "region_name": s3_creds.region_name,
        "max_pool_connections": s3_config.max_pool_connections,
        "read_timeout": s3_config.timeout_read,
        "connect_timeout": s3_config.timeout_connection,
    }
    if payload_signing_enabled is not None:
        config_kwargs["s3"] = {"payload_signing_enabled": payload_signing_enabled}
    return session.client(
        service_name="s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        endpoint_url=endpoint_url,
        config=Config(**config_kwargs),
    )


def _create_backblaze_s3_client(s3_creds: S3Credentials, s3_config: S3Config) -> BaseClient:
    """Create and return an S3 client."""
    return _create_s3_client(
        s3_creds,
        s3_config,
        default_endpoint=_DEFAULT_BACKBLAZE_ENDPOINT,
        payload_signing_enabled=False,
    )


def _create_unknown_s3_client(s3_creds: S3Credentials, s3_config: S3Config) -> BaseClient:
    """Create and return an S3 client."""
    return _create_s3_client(s3_creds, s3_config, normalize_schema_naive_endpoint=True)


def create_s3_client(s3_creds: S3Credentials, s3_config: S3Config | None = None) -> BaseClient:
    """Create and return an S3 client."""
    s3_config = s3_config or S3Config()
    provider = s3_creds.provider
    if provider == S3Provider.BACKBLAZE:
        if s3_config.verbose:
            logger.info("Creating BackBlaze S3 client")
        return _create_backblaze_s3_client(s3_creds=s3_creds, s3_config=s3_config)
    else:
        if s3_config.verbose:
            logger.info("Creating generic/unknown S3 client")
        return _create_unknown_s3_client(s3_creds=s3_creds, s3_config=s3_config)
