from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from rclone_kit.config import Config, Parsed, Section
from rclone_kit.s3.types import S3Credentials, S3Provider
from rclone_kit.types import S3PathInfo
from rclone_kit.util import get_verbose

if TYPE_CHECKING:
    from rclone_kit.rc.client import RcCallable

logger = logging.getLogger(__name__)


def fetch_config_paths_embedded(rc_client: RcCallable) -> list[Path]:
    """Return the filesystem paths reported by `config/paths`: the config
    file, cache directory, and temp directory, in that fixed order.
    """
    result = rc_client.call("config/paths")
    return [
        Path(value)
        for value in (result.get("config"), result.get("cache"), result.get("temp"))
        if value
    ]


def fetch_config_show_embedded(rc_client: RcCallable, remote: str | None = None) -> str:
    """Return the configuration text reported by `rclonekit/configshow`,
    byte-for-byte identical to `rclone config show`/`rclone config show
    <remote>`'s own plain-text output.
    """
    params: dict[str, object] = {}
    if remote is not None:
        params["remote"] = remote
    result = rc_client.call("rclonekit/configshow", **params)
    text = result["text"]
    if not isinstance(text, str):
        raise ValueError(f"text must be a string, got {text!r}")
    return text


def check_is_s3(config: Config, dst: str) -> bool:
    """Check if a remote is an S3 remote."""
    try:
        path_info: S3PathInfo = S3PathInfo.from_str(dst)
        remote = path_info.remote
        parsed: Parsed = config.parse()
        sections: dict[str, Section] = parsed.sections
        if remote not in sections:
            return False
        section: Section = sections[remote]
        t = section.type()
        return t in ["s3", "b2"]
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logging.exception(f"Error checking if remote is S3: {e}")
        return False


def fetch_s3_credentials(config: Config, remote: str, verbose: bool | None = None) -> S3Credentials:
    verbose = get_verbose(verbose)
    path_info: S3PathInfo = S3PathInfo.from_str(remote)

    remote = path_info.remote
    bucket_name = path_info.bucket

    parsed: Parsed = config.parse()
    sections: dict[str, Section] = parsed.sections
    if remote not in sections:
        raise ValueError(
            f"Remote {remote} not found in rclone config, remotes are: {sections.keys()}"
        )

    section: Section = sections[remote]
    dst_type = section.type()
    if dst_type not in {"s3", "b2"}:
        raise ValueError(f"Remote {remote} is not an S3 remote, it is of type {dst_type}")

    def get_provider_str(section=section) -> str | None:
        type: str = section.type()
        provider: str | None = section.provider()
        if provider is not None:
            return provider
        if type == "b2":
            return S3Provider.BACKBLAZE.value
        if type != "s3":
            raise ValueError(f"Remote {remote} is not an S3 remote")
        return S3Provider.S3.value

    provider: str
    if provided_provider_str := get_provider_str():
        if verbose:
            logger.info("Using provided provider: %s", provided_provider_str)
        provider = provided_provider_str
    else:
        if verbose:
            logger.info("Using default provider: %s", S3Provider.S3.value)
        provider = S3Provider.S3.value
    provider_enum = S3Provider.from_str(provider)

    s3_creds: S3Credentials = S3Credentials(
        bucket_name=bucket_name,
        provider=provider_enum,
        access_key_id=section.access_key_id(),
        secret_access_key=section.secret_access_key(),
        endpoint_url=section.endpoint(),
    )
    return s3_creds
