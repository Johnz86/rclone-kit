from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from rclone_kit.backend import RcloneBackend
from rclone_kit.config import Config, Parsed, Section
from rclone_kit.config_discovery import parse_rclone_paths
from rclone_kit.exceptions import RcloneCommandError
from rclone_kit.s3.types import S3Credentials, S3Provider
from rclone_kit.types import S3PathInfo
from rclone_kit.util import get_verbose

logger = logging.getLogger(__name__)


def obscure_password(backend: RcloneBackend, password: str) -> str:
    """Obscure a password for use in rclone config files."""
    cmd_list: list[str] = ["obscure", password]
    cp = backend.run(tuple(cmd_list))
    return cp.stdout.strip()


def fetch_config_paths(
    backend: RcloneBackend,
    remote: str | None = None,
    obscure: bool = False,
    no_obscure: bool = False,
) -> list[Path]:
    """Return the filesystem paths reported by `rclone config paths`:
    the config file, cache directory, and temp directory, in that fixed
    order.

    `remote`, `obscure`, and `no_obscure` are accepted for backward
    compatibility with this method's public signature. `config paths`
    takes no such arguments upstream, so they are ignored.

    Raises:
        RcloneCommandError: if the underlying `rclone config paths`
            invocation fails.
    """
    del remote, obscure, no_obscure
    cmd_list: list[str] = ["config", "paths"]

    try:
        cp = backend.run(tuple(cmd_list), capture=True, check=True)
    except subprocess.CalledProcessError as error:
        raise RcloneCommandError("config paths", error.stderr or "", error) from error
    stdout: str | bytes = cp.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8")
    return parse_rclone_paths(stdout).present_paths()


def fetch_config_show(
    backend: RcloneBackend,
    remote: str | None = None,
    obscure: bool = False,
    no_obscure: bool = False,
) -> str:
    """Return the configuration text reported by `rclone config show`.

    Raises:
        ValueError: if both `obscure` and `no_obscure` are set.
        RcloneCommandError: if the underlying `rclone config show`
            invocation fails.
    """
    if obscure and no_obscure:
        raise ValueError("obscure and no_obscure cannot both be enabled")
    cmd_list = ["config", "show"]
    if remote is not None:
        cmd_list.append(remote)
    if obscure:
        cmd_list.append("--obscure")
    if no_obscure:
        cmd_list.append("--no-obscure")
    try:
        cp = backend.run(tuple(cmd_list), capture=True, check=True)
    except subprocess.CalledProcessError as error:
        raise RcloneCommandError("config show", error.stderr or "", error) from error
    stdout = cp.stdout
    return stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout


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
