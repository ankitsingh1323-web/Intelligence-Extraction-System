"""S3-compatible client for the optional OBS (object storage) upload
mirror -- boto3's S3 client talks to any S3-compatible endpoint (OBS,
MinIO, Ceph, ...) via a custom endpoint_url, so no vendor-specific SDK is
needed. See storage/obs_settings.py for where credentials/the enabled
flag live, and why the secret key never leaves this process decrypted.
"""
import asyncio
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings
from app.storage.obs_settings import (
    OBSCredentialFull,
    get_active_credential_full,
    get_credential_full,
    is_enabled,
    mark_verified,
)

logger = logging.getLogger(__name__)


class CredentialNotFoundError(Exception):
    """Raised by list_objects/download_object (not by test_credential or
    mirror_upload, which are best-effort and never raise) -- both are
    user-initiated actions against a specific credential_id/prefix the
    user typed, so a missing credential needs to surface as a real 404,
    not be silently swallowed the way the upload-mirror's "no active
    credential" case is."""


def _require_credential(credential_id: str) -> OBSCredentialFull:
    credential = get_credential_full(credential_id)
    if credential is None:
        raise CredentialNotFoundError(credential_id)
    return credential


def _normalize_endpoint(endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return f"https://{endpoint}"


def _client_for(credential: OBSCredentialFull):
    settings = get_settings()
    boto_config = Config(
        connect_timeout=settings.obs_connect_timeout_s,
        read_timeout=settings.obs_read_timeout_s,
        # A single attempt, not boto3's default retry-with-backoff --
        # this call sits in the critical path of an upload request (or a
        # user-initiated "test connection" click); a hung/unreachable
        # bucket should fail fast and visibly, not silently retry for
        # much longer than the configured timeouts already suggest.
        retries={"max_attempts": 1},
    )
    session = boto3.session.Session()
    return session.client(
        "s3",
        endpoint_url=_normalize_endpoint(credential.endpoint),
        aws_access_key_id=credential.access_key,
        aws_secret_access_key=credential.secret_key,
        # boto3 requires SOME region even against a fully custom
        # S3-compatible endpoint that doesn't use AWS regions at all --
        # falls back to a harmless placeholder when the credential didn't
        # specify one.
        region_name=credential.region or "us-east-1",
        config=boto_config,
    )


def _object_key(credential: OBSCredentialFull, job_id: str, filename: str) -> str:
    prefix = credential.path_prefix.strip("/")
    parts = [p for p in (prefix, job_id, filename) if p]
    return "/".join(parts)


def _check_bucket(credential: OBSCredentialFull) -> tuple[bool, str]:
    client = _client_for(credential)
    try:
        client.head_bucket(Bucket=credential.bucket)
        return True, f'Reachable — bucket "{credential.bucket}" is accessible with this credential.'
    except ClientError as exc:
        error = exc.response.get("Error", {})
        return False, f"Bucket check failed ({error.get('Code', 'Unknown')}): {error.get('Message', str(exc))}"
    except BotoCoreError as exc:
        return False, f"Could not reach endpoint: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error testing connection: {exc}"


async def test_credential(credential_id: str) -> tuple[bool, str]:
    """Runs a real HeadBucket call -- the standard S3-compatible way to
    check both reachability and read permission without listing or
    touching any objects. Offloaded to a thread (boto3 is synchronous) so
    it never blocks the event loop; the Config in _client_for bounds how
    long an unreachable endpoint can hang this for. Records the result
    via mark_verified so it shows up next to the credential in the
    Settings UI without the frontend needing a second round-trip.
    """
    credential = get_credential_full(credential_id)
    if credential is None:
        return False, "Credential not found."
    ok, detail = await asyncio.to_thread(_check_bucket, credential)
    mark_verified(credential_id, ok, detail)
    return ok, detail


async def mirror_upload(job_id: str, filename: str, content: bytes) -> None:
    """Best-effort: pushes a copy of an uploaded file to the active OBS
    credential's bucket, if OBS uploads are enabled and a credential is
    marked active. Never raises -- callers (routes_ingest.py) treat this
    exactly like this app's other optional enrichment steps (translation,
    the dataset library mirror): log and move on, never block or fail the
    upload the user is actually waiting on because of a bucket problem.
    """
    if not is_enabled():
        return
    credential = get_active_credential_full()
    if credential is None:
        return

    def _put() -> None:
        client = _client_for(credential)
        key = _object_key(credential, job_id, filename)
        client.put_object(Bucket=credential.bucket, Key=key, Body=content)

    try:
        await asyncio.to_thread(_put)
    except Exception:
        logger.exception(
            "OBS mirror upload failed for %s (job %s) -- file was still saved to local disk normally.",
            filename, job_id,
        )


async def list_objects(credential_id: str, prefix: str, max_keys: int = 500) -> tuple[list[dict], bool]:
    """Lists objects under `prefix` in the credential's bucket -- the
    browse step behind the Upload page's "start analysis from object
    storage" flow (list what's there, the user ticks which ones to bring
    in, ingest_from_obs in routes_ingest.py downloads exactly those).

    Unlike mirror_upload/test_credential, this is a user-initiated action
    against a specific bucket/prefix they typed, so a real failure here
    (bad credential, unreachable endpoint, no list permission) needs to
    reach them as an actual error -- raises CredentialNotFoundError or
    whatever boto3 exception the listing call itself produces, rather
    than swallowing it.

    Returns (objects, truncated) -- truncated is True when the bucket has
    more than max_keys matching objects, so the caller can tell the user
    to narrow the prefix instead of silently showing a partial list as
    if it were everything.
    """
    credential = _require_credential(credential_id)

    def _list() -> tuple[list[dict], bool]:
        client = _client_for(credential)
        resp = client.list_objects_v2(Bucket=credential.bucket, Prefix=prefix, MaxKeys=max_keys)
        objects = [
            {
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat() if obj.get("LastModified") else "",
            }
            for obj in resp.get("Contents", [])
            # Skip zero-byte "directory marker" keys ending in "/" that
            # some tools create to represent an empty folder -- nothing
            # there to download or analyze.
            if not obj["Key"].endswith("/")
        ]
        return objects, bool(resp.get("IsTruncated"))

    return await asyncio.to_thread(_list)


async def download_object(credential_id: str, key: str) -> bytes:
    """Downloads one object's bytes, for pulling a selected file down into
    the job's own local upload directory (see routes_ingest.py's
    ingest_from_obs) so the rest of the pipeline -- parsing, extraction,
    the dataset library, the OBS upload mirror itself -- works identically
    regardless of whether a file arrived via browser upload or from a
    bucket. Same "must raise, not swallow" reasoning as list_objects."""
    credential = _require_credential(credential_id)

    def _get() -> bytes:
        client = _client_for(credential)
        response = client.get_object(Bucket=credential.bucket, Key=key)
        return response["Body"].read()

    return await asyncio.to_thread(_get)
