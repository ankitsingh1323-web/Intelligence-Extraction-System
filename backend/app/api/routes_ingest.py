import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import get_settings
from app.models.schemas import FileProgress, Job, JobStatus
from app.parsers.archive_expand import expand_archive, is_archive
from app.parsers.router import classify
from app.pipeline.job_manager import JobManager, get_job_manager
from app.storage import obs_client
from app.storage.file_store import save_upload
from app.storage.obs_client import CredentialNotFoundError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ingest"])


async def _finalize_and_start(
    job: Job, saved_paths: list[str], manager: JobManager, obs_metadata: dict[str, dict] | None = None,
) -> Job:
    """Shared by both ingest paths (browser upload and object storage)
    once every file is sitting in the job's local upload directory --
    from here on the pipeline doesn't care where the bytes originally
    came from. Archive Agent (L2): expands any ZIP/TAR into its member
    files before the job's file list is finalized, so each extracted file
    flows through the normal per-file pipeline like anything else; the
    archive itself is never processed as a "file," only its contents are.

    obs_metadata (only ever non-empty from ingest_from_obs) maps a saved
    path to its OBS provenance -- source_path/source_folder_path/
    translated_*, see FileProgress. Keyed by saved path rather than
    final_path so an OBS-sourced archive's own metadata doesn't need to
    somehow apply to each of its extracted members individually (it
    doesn't try to -- an archive's extracted files simply get none, same
    gap that already exists for archives from a regular upload).
    """
    final_paths: list[str] = []
    archive_warnings: list[str] = []
    for path in saved_paths:
        if is_archive(path):
            extract_dir = str(Path(path).parent / "extracted")
            # expand_archive does synchronous disk I/O and can decompress a
            # sizeable amount of data -- running it directly on the event
            # loop would block every other in-flight request (job status
            # polling, other uploads) for as long as extraction takes.
            members, warnings = await asyncio.to_thread(expand_archive, path, extract_dir)
            archive_warnings.extend(warnings)
            if not members:
                archive_warnings.append(f"{Path(path).name}: archive produced no usable files.")
            final_paths.extend(members)
        else:
            final_paths.append(path)

    obs_metadata = obs_metadata or {}
    job.files = []
    for p in final_paths:
        meta = obs_metadata.get(p, {})
        job.files.append(FileProgress(
            filename=p, category=classify(p), status=JobStatus.QUEUED,
            source_path=meta.get("source_path"),
            source_folder_path=meta.get("source_folder_path"),
            translated_source_path=meta.get("translated_source_path"),
            translated_folder_path=meta.get("translated_folder_path"),
        ))
    job.warnings = archive_warnings

    manager.start(job.job_id, final_paths)
    return job


@router.post("/ingest", response_model=Job)
async def ingest_files(files: list[UploadFile]) -> Job:
    if not files:
        raise HTTPException(400, "No files provided.")

    settings = get_settings()
    manager = get_job_manager()

    # Job id is needed before files are saved so uploads land in a per-job dir.
    job = manager.create_job(file_paths=[f.filename or "unnamed" for f in files])

    saved_paths: list[str] = []
    for f in files:
        content = await f.read()
        if len(content) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"{f.filename} exceeds max upload size of {settings.max_upload_mb} MB.")
        filename = f.filename or "unnamed"
        path = await save_upload(job.job_id, filename, content)
        saved_paths.append(path)
        # Optional OBS mirror (see storage/obs_client.py) -- fire-and-forget,
        # not awaited: mirror_upload already no-ops instantly when the
        # feature is off (the default) or no credential is active, and
        # when it IS active this must never make the upload response wait
        # on a possibly slow/unreachable external bucket. The function
        # swallows its own errors and just logs, so there's nothing here
        # to await or handle.
        asyncio.create_task(obs_client.mirror_upload(job.job_id, filename, content))

    return await _finalize_and_start(job, saved_paths, manager)


class ObsIngestRequest(BaseModel):
    credential_id: str
    keys: list[str]


def _local_filename_for_key(key: str) -> str:
    """Object keys can repeat a basename across different "folders"
    (prefixes) within the same bucket -- save_upload writes into one flat
    per-job directory, so a bare basename collision would silently
    overwrite one selected file with another. Flattening the whole key
    (slashes -> double underscore) keeps every key's local filename
    unique by construction (bucket keys are themselves unique) while
    still showing which folder a file came from in the UI."""
    return key.strip("/").replace("/", "__") or "unnamed"


@router.post("/ingest/obs", response_model=Job)
async def ingest_from_obs(body: ObsIngestRequest) -> Job:
    """Starts a new analysis job from files already sitting in an OBS
    bucket instead of a browser upload -- the "From Object Storage" tab
    on the Upload page (see routes_settings.browse_obs_credential for the
    listing step this consumes). Downloads every selected object BEFORE
    creating the job (unlike ingest_files, which needs a job_id upfront
    to stream browser uploads into a per-job dir): a download failure
    partway through then fails the whole request cleanly, before any
    job/local state exists to be left orphaned, rather than leaving a
    half-populated job sitting in the registry with no files and nothing
    processing it.
    """
    if not body.keys:
        raise HTTPException(400, "No objects selected.")
    settings = get_settings()

    downloads: list[tuple[str, bytes, str]] = []  # (local filename, content, original OBS key)
    for key in body.keys:
        try:
            content = await obs_client.download_object(body.credential_id, key)
        except CredentialNotFoundError:
            raise HTTPException(404, "Credential not found.")
        except Exception as exc:  # noqa: BLE001 -- a real download failure, surface it plainly
            raise HTTPException(502, f'Failed to download "{key}" from object storage: {exc}')
        if len(content) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f'"{key}" exceeds max upload size of {settings.max_upload_mb} MB.')
        downloads.append((_local_filename_for_key(key), content, key))

    # The bucket's folder structure is metadata worth keeping beyond the
    # flattened local filename above, and its folder/file names may be in
    # a different language than the pipeline's target (a common real case:
    # Arabic folder names on an on-prem OBS deployment) -- translate the
    # distinct segment set across just the selected keys in one batch
    # (see agents/translation.translate_path_segments) rather than
    # per-file, then attach both the original and translated breadcrumb
    # to each file's FileProgress below.
    from app.agents.translation import translate_path_segments

    parsed: dict[str, tuple[list[str], str]] = {}
    all_segments: set[str] = set()
    for _, _, key in downloads:
        folder_path, filename = key.strip("/").split("/")[:-1], key.strip("/").split("/")[-1]
        parsed[key] = (folder_path, filename)
        all_segments.update(folder_path)
        all_segments.add(filename)
    translations = await translate_path_segments(sorted(all_segments))

    manager = get_job_manager()
    job = manager.create_job(file_paths=[name for name, _, _ in downloads])

    obs_metadata: dict[str, dict] = {}
    saved_paths: list[str] = []
    for name, content, key in downloads:
        saved_path = await save_upload(job.job_id, name, content)
        saved_paths.append(saved_path)
        folder_path, filename = parsed[key]
        translated_folder = [translations.get(p, p) for p in folder_path]
        translated_filename = translations.get(filename, filename)
        was_translated = translated_folder != folder_path or translated_filename != filename
        obs_metadata[saved_path] = {
            "source_path": key,
            "source_folder_path": folder_path,
            "translated_source_path": "/".join([*translated_folder, translated_filename]) if was_translated else None,
            "translated_folder_path": translated_folder if was_translated else None,
        }

    # No OBS mirror here -- every one of these files already lives in this
    # exact bucket; pushing a copy of it back to itself would be pointless.
    return await _finalize_and_start(job, saved_paths, manager, obs_metadata)
