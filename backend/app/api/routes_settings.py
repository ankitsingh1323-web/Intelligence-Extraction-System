"""Deployment-level settings -- currently just the optional OBS (object
storage) upload mirror. Distinct from config.Settings (env-var, read-only
at process start): these are runtime-mutable, saved via
storage/obs_settings.py, and editable from the frontend's Settings page.
"""
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.models.schemas import OBSBrowseResult, OBSCredentialPublic, OBSObjectSummary, OBSSettings
from app.storage import obs_client, obs_settings
from app.storage.obs_client import CredentialNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


class SetEnabledRequest(BaseModel):
    enabled: bool


class CreateCredentialRequest(BaseModel):
    name: str
    endpoint: str
    region: str | None = None
    bucket: str
    path_prefix: str = ""
    access_key: str
    secret_key: str


class UpdateCredentialRequest(BaseModel):
    """All fields optional -- only what's provided changes (see
    obs_settings.update_credential). Omitting secret_key keeps the
    previously-saved one; there's no way to submit "no secret key" for a
    credential that already has one, by design."""
    name: str | None = None
    endpoint: str | None = None
    region: str | None = None
    bucket: str | None = None
    path_prefix: str | None = None
    access_key: str | None = None
    secret_key: str | None = None


class TestResult(BaseModel):
    ok: bool
    detail: str


def _current_settings() -> OBSSettings:
    return OBSSettings(enabled=obs_settings.is_enabled(), credentials=obs_settings.list_credentials())


@router.get("/obs", response_model=OBSSettings)
async def get_obs_settings() -> OBSSettings:
    return _current_settings()


@router.patch("/obs", response_model=OBSSettings)
async def set_obs_enabled(body: SetEnabledRequest) -> OBSSettings:
    obs_settings.set_enabled(body.enabled)
    return _current_settings()


@router.post("/obs/credentials", response_model=OBSCredentialPublic)
async def create_obs_credential(body: CreateCredentialRequest) -> OBSCredentialPublic:
    return obs_settings.create_credential(
        name=body.name, endpoint=body.endpoint, region=body.region, bucket=body.bucket,
        path_prefix=body.path_prefix, access_key=body.access_key, secret_key=body.secret_key,
    )


@router.put("/obs/credentials/{credential_id}", response_model=OBSCredentialPublic)
async def update_obs_credential(credential_id: str, body: UpdateCredentialRequest) -> OBSCredentialPublic:
    updated = obs_settings.update_credential(
        credential_id, name=body.name, endpoint=body.endpoint, region=body.region, bucket=body.bucket,
        path_prefix=body.path_prefix, access_key=body.access_key, secret_key=body.secret_key,
    )
    if updated is None:
        raise HTTPException(404, "Credential not found.")
    return updated


@router.delete("/obs/credentials/{credential_id}", status_code=204)
async def delete_obs_credential(credential_id: str) -> None:
    deleted = obs_settings.delete_credential(credential_id)
    if not deleted:
        raise HTTPException(404, "Credential not found.")


@router.post("/obs/credentials/{credential_id}/activate", response_model=OBSSettings)
async def activate_obs_credential(credential_id: str) -> OBSSettings:
    activated = obs_settings.set_active(credential_id)
    if not activated:
        raise HTTPException(404, "Credential not found.")
    return _current_settings()


@router.post("/obs/credentials/{credential_id}/test", response_model=TestResult)
async def test_obs_credential(credential_id: str) -> TestResult:
    ok, detail = await obs_client.test_credential(credential_id)
    return TestResult(ok=ok, detail=detail)


@router.get("/obs/credentials/{credential_id}/browse", response_model=OBSBrowseResult)
async def browse_obs_credential(credential_id: str, prefix: str = Query("")) -> OBSBrowseResult:
    """Backs the Upload page's "start analysis from object storage" flow
    -- lists what's under `prefix` in this credential's bucket so the
    user can tick which files to bring into a new job (see
    routes_ingest.ingest_from_obs, which downloads exactly the keys
    selected here)."""
    try:
        objects, truncated = await obs_client.list_objects(credential_id, prefix)
    except CredentialNotFoundError:
        raise HTTPException(404, "Credential not found.")
    except Exception as exc:  # noqa: BLE001 -- a real bucket/network failure, surface it plainly
        raise HTTPException(502, f"Could not list objects: {exc}")
    return OBSBrowseResult(
        objects=[OBSObjectSummary(**obj) for obj in objects],
        truncated=truncated,
    )
