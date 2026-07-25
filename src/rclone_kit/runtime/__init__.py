"""Runtime package supporting the embedded native library.

Public surface:

- `rclone_kit.runtime.platform`: the data-driven operating-system and
  architecture model, plus platform normalization.
- `rclone_kit.runtime.native_platform`: the certified native (C ABI) build
  target model, used by the native build/packaging pipeline.
- `rclone_kit.runtime.hashing`: `sha256_of_file`, used to verify the bundled
  native library.
- `rclone_kit.runtime.exceptions`: every exception raised by this package.

Import from the specific submodule rather than this package's namespace to
keep import graphs explicit.
"""
