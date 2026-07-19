import warnings

from botocore.client import BaseClient

from rclone_kit.s3.basic_ops import (
    download_file,
    head,
    list_bucket_contents,
    upload_file,
)
from rclone_kit.s3.create import S3Config, create_s3_client
from rclone_kit.s3.multipart.upload_parts_inline import (
    MultiUploadResult,
    upload_file_multipart,
)
from rclone_kit.s3.types import S3Credentials, S3MutliPartUploadConfig, S3UploadTarget

_MIN_THRESHOLD_FOR_CHUNKING = 5 * 1024 * 1024


class S3Client:
    def __init__(self, s3_creds: S3Credentials, verbose: bool = False) -> None:
        self.verbose = verbose
        self.credentials: S3Credentials = s3_creds
        self.client: BaseClient = create_s3_client(
            s3_creds=s3_creds, s3_config=S3Config(verbose=verbose)
        )

    @property
    def bucket_name(self) -> str:
        return self.credentials.bucket_name

    def list_bucket_contents(self, bucket_name: str) -> None:
        list_bucket_contents(self.client, bucket_name)

    def upload_file(self, target: S3UploadTarget) -> None:
        bucket_name = target.bucket_name
        file_path = target.src_file
        object_name = target.s3_key
        upload_file(
            s3_client=self.client,
            bucket_name=bucket_name,
            file_path=file_path,
            object_name=object_name,
        )

    def download_file(self, bucket_name: str, object_name: str, file_path: str) -> None:
        download_file(self.client, bucket_name, object_name, file_path)

    def head(self, bucket_name: str, object_name: str) -> dict | None:
        return head(self.client, bucket_name, object_name)

    def upload_file_multipart(
        self,
        upload_target: S3UploadTarget,
        upload_config: S3MutliPartUploadConfig,
    ) -> MultiUploadResult:

        chunk_size = upload_config.chunk_size
        retries = upload_config.retries
        resume_path_json = upload_config.resume_path_json
        max_chunks_before_suspension = upload_config.max_chunks_before_suspension
        bucket_name = upload_target.bucket_name

        try:
            if upload_target.src_file_size is None:
                filesize = upload_target.src_file.stat().st_size
            else:
                filesize = upload_target.src_file_size

            if filesize < _MIN_THRESHOLD_FOR_CHUNKING:
                warnings.warn(
                    f"File size {filesize} is less than the minimum threshold for chunking ({_MIN_THRESHOLD_FOR_CHUNKING}), switching to single threaded upload.",
                    stacklevel=2,
                )
                self.upload_file(upload_target)
                return MultiUploadResult.UPLOADED_FRESH

            out = upload_file_multipart(
                s3_client=self.client,
                chunk_fetcher=upload_config.chunk_fetcher,
                bucket_name=bucket_name,
                file_path=upload_target.src_file,
                file_size=filesize,
                object_name=upload_target.s3_key,
                resumable_info_path=resume_path_json,
                chunk_size=chunk_size,
                retries=retries,
                max_chunks_before_suspension=max_chunks_before_suspension,
            )
            return out
        except Exception as e:
            key = upload_target.s3_key
            endpoint_url = self.credentials.endpoint_url
            provider = self.credentials.provider.value
            region_name = self.credentials.region_name
            warnings.warn(
                "Error uploading file "
                f"{key!r} to bucket {bucket_name!r} via {provider!r} "
                f"at {endpoint_url!r} in region {region_name!r}: {type(e).__name__}",
                stacklevel=2,
            )
            raise
