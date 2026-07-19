import logging
from pathlib import Path

from botocore.client import BaseClient

logger = logging.getLogger(__name__)


def list_bucket_contents(s3_client: BaseClient, bucket_name: str) -> None:
    """List contents of the specified bucket."""
    try:
        response = s3_client.list_objects_v2(Bucket=bucket_name)
        if "Contents" in response:
            for obj in response["Contents"]:
                logger.info("File: %s | Size: %s bytes", obj["Key"], obj["Size"])
        else:
            logger.info("The bucket '%s' is empty.", bucket_name)
    except Exception as e:
        logger.error("Error listing bucket contents: %s", e)


def upload_file(
    s3_client: BaseClient,
    bucket_name: str,
    file_path: Path,
    object_name: str,
) -> None:
    """Upload a file to the bucket."""
    s3_client.upload_file(str(file_path), bucket_name, object_name)
    logger.info("Uploaded %s to %s/%s", file_path, bucket_name, object_name)


def download_file(
    s3_client: BaseClient, bucket_name: str, object_name: str, file_path: str
) -> None:
    """Download a file from the bucket."""
    try:
        s3_client.download_file(bucket_name, object_name, file_path)
        logger.info("Downloaded %s from %s to %s", object_name, bucket_name, file_path)
    except Exception as e:
        logger.error("Error downloading file: %s", e)


def head(s3_client: BaseClient, bucket_name: str, object_name: str) -> dict | None:
    """
    Retrieve metadata for the specified object using a HEAD operation.

    :param s3_client: The S3 client to use.
    :param bucket_name: The name of the bucket containing the object.
    :param object_name: The key of the object.
    :return: A dictionary containing the object's metadata if successful, otherwise None.
    """
    try:
        response = s3_client.head_object(Bucket=bucket_name, Key=object_name)
        logger.info("Metadata for %s in %s: %s", object_name, bucket_name, response)
        return response
    except Exception as e:
        logger.error("Error retrieving metadata for %s: %s", object_name, e)
        return None
