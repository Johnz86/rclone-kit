"""Runtime package and resolution for the bundled rclone executable.

Public surface:

- `rclone_kit.runtime.platform`: the data-driven operating-system,
  architecture, and `RcloneArtifact` model, plus platform normalization.
- `rclone_kit.runtime.rclone_binary`: `resolve_rclone_executable`, the sole
  entry point for locating a usable rclone executable.
- `rclone_kit.runtime.downloader`: `fetch_verified_archive`, the verified
  HTTPS downloader for immutable rclone release archives.
- `rclone_kit.runtime.exceptions`: every exception raised by this package.

Import from the specific submodule rather than this package's namespace to
keep import graphs explicit.
"""
