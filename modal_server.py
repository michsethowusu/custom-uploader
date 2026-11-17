"""Modal backend for the LEIN Speech radio-station upload portal.

Serves a tus resumable-upload endpoint to the static Hugging Face Space, stages
chunks only temporarily on a small Modal volume, and passes every completed file
straight into the public Hugging Face dataset. Nothing is permanently stored on
Modal: the staged chunk file is deleted immediately after the Hub commit.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import modal

APP_NAME = "lein-speech-upload"
VOLUME_NAME = "lein-speech-uploads"
STAGING_ROOT = Path("/staging")
UPLOADS_ROOT = STAGING_ROOT / "uploads"

CHUNK_SIZE = 16 * 1024 * 1024
MAX_FILE_SIZE = 5 * 1024**3
MAX_META_VALUE = 1000
STALE_UPLOAD_HOURS = 24
ALLOWED_EXTENSIONS = {
    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".webm",
    ".zip", ".csv", ".json", ".txt",
}

volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True, version=2)
hub_secret = modal.Secret.from_name("huggingface")
config_secret = modal.Secret.from_name("lein-speech-upload")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi>=0.115,<1", "huggingface_hub>=0.36,<2", "uvicorn>=0.30")
)

app = modal.App(APP_NAME, image=image)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip().lower()
    return re.sub(r"[-\s]+", "-", value)[:80] or "unknown"


def _safe_filename(value: str) -> str:
    value = value.replace("\\", "/").split("/")[-1]
    value = re.sub(r"[^\w.\- ()]", "_", value, flags=re.UNICODE)
    return value.strip(". ")[:180] or "upload.bin"


def _safe_relative_path(value: str | None, fallback: str) -> str:
    raw = (value or fallback).replace("\\", "/")
    parts = []
    for part in raw.split("/"):
        part = part.strip()
        if not part or part in {".", ".."}:
            continue
        clean = _safe_filename(part)
        if clean and clean not in {".", ".."}:
            parts.append(clean)
    if not parts:
        parts = [_safe_filename(fallback)]
    return "/".join(parts[:20])[:600]


def _parse_upload_metadata(header: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not header:
        return result
    for item in header.split(","):
        if not item.strip():
            continue
        key, _, encoded = item.strip().partition(" ")
        try:
            result[key.lower()] = base64.b64decode(encoded + "===").decode("utf-8", "replace")
        except Exception:
            continue
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _reload_volume() -> None:
    try:
        volume.reload()
    except Exception:
        pass


def _commit_volume() -> None:
    volume.commit()


def _upload_to_hub(api: Any, payload: Any, remote_path: str, message: str) -> None:
    repo = os.environ["LEIN_DATASET_REPO"]
    for attempt in range(1, 6):
        try:
            api.upload_file(
                path_or_fileobj=payload,
                path_in_repo=remote_path,
                repo_id=repo,
                repo_type="dataset",
                commit_message=message,
            )
            return
        except Exception:
            if attempt == 5:
                raise
            time.sleep(2 * attempt)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@app.function(
    image=image,
    secrets=[hub_secret, config_secret],
    volumes={str(STAGING_ROOT): volume},
    timeout=15 * 60,
    retries=1,
)
def finalize_upload(upload_id: str) -> dict[str, Any]:
    """Move one fully staged file into the public HF dataset, then free staging."""
    from huggingface_hub import HfApi

    _reload_volume()
    folder = UPLOADS_ROOT / upload_id
    meta_path = folder / "meta.json"
    data_path = folder / "data.bin"
    done_path = folder / "done.json"
    if not meta_path.exists():
        return {"status": "error", "error": "upload metadata not found"}

    meta = _read_json(meta_path)
    if not data_path.exists() or data_path.stat().st_size != int(meta["length"]):
        return {"status": "error", "error": "staged file is incomplete"}

    try:
        checksum = _sha256_file(data_path)
        stamp = datetime.now(timezone.utc)
        month = stamp.strftime("%Y-%m")
        station_slug = meta["station_slug"]
        safe_name = meta["safe_filename"]
        stem = Path(safe_name).stem
        prefix = checksum[:12]

        relative_path = meta.get("relative_path") or safe_name
        parts = [part for part in relative_path.split("/") if part]
        folder_prefix = "/".join(parts[:-1])
        audio_base = f"uploads/{station_slug}/{month}"
        metadata_base = f"metadata/{station_slug}/{month}"
        audio_path = (
            f"{audio_base}/{folder_prefix}/{prefix}-{safe_name}"
            if folder_prefix else f"{audio_base}/{prefix}-{safe_name}"
        )
        metadata_path = (
            f"{metadata_base}/{folder_prefix}/{prefix}-{stem}.json"
            if folder_prefix else f"{metadata_base}/{prefix}-{stem}.json"
        )

        api = HfApi(token=os.environ["HF_TOKEN"])
        if not api.file_exists(
            repo_id=os.environ["LEIN_DATASET_REPO"],
            filename=audio_path,
            repo_type="dataset",
        ):
            _upload_to_hub(
                api,
                str(data_path),
                audio_path,
                f"Add station upload from {meta['station']}",
            )

        record = {
            "upload_id": upload_id,
            "submission_id": meta.get("submission_id", ""),
            "station": meta["station"],
            "email": meta.get("email", ""),
            "language": meta.get("language", ""),
            "original_filename": meta["original_filename"],
            "original_relative_path": meta.get("original_relative_path", ""),
            "relative_path": relative_path,
            "content_type": meta.get("content_type", ""),
            "size_bytes": int(meta["length"]),
            "sha256": checksum,
            "uploaded_at": _utc_now(),
            "audio_path": audio_path,
        }
        _upload_to_hub(
            api,
            json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
            metadata_path,
            f"Add metadata for station upload {prefix}",
        )

        dataset = os.environ["LEIN_DATASET_REPO"]
        result = {
            "status": "completed",
            "dataset_url": f"https://huggingface.co/datasets/{dataset}",
            "file_url": f"https://huggingface.co/datasets/{dataset}/blob/main/{audio_path}",
            "audio_path": audio_path,
            "sha256": checksum,
            "completed_at": _utc_now(),
        }
        _write_json(done_path, result)
        data_path.unlink(missing_ok=True)
        _commit_volume()
        return result
    except Exception as exc:
        _write_json(done_path, {
            "status": "failed",
            "error": str(exc)[:500],
            "failed_at": _utc_now(),
        })
        _commit_volume()
        raise


@app.function(
    image=image,
    secrets=[hub_secret, config_secret],
    timeout=5 * 60,
    retries=2,
)
def record_submission(payload: dict[str, Any]) -> str:
    """Persist the station form details once per upload session."""
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ["HF_TOKEN"])
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    path = f"submissions/{payload['station_slug']}/{month}/{payload['submission_id']}.json"
    document = {
        "submission_id": payload["submission_id"],
        "station": payload["station"],
        "email": payload["email"],
        "language": payload["language"],
        "recorded_at": _utc_now(),
    }
    _upload_to_hub(
        api,
        json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"),
        path,
        f"Record submission from {payload['station']}",
    )
    return path


@app.function(
    image=image,
    secrets=[hub_secret, config_secret],
    volumes={str(STAGING_ROOT): volume},
    schedule=modal.Period(hours=6),
    timeout=10 * 60,
)
def cleanup_stale_uploads() -> int:
    """Delete partial staging directories older than the retention window."""
    _reload_volume()
    UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_UPLOAD_HOURS)
    removed = 0
    for folder in UPLOADS_ROOT.iterdir():
        if not folder.is_dir():
            continue
        try:
            created = datetime.fromisoformat(_read_json(folder / "meta.json")["created_at"])
        except Exception:
            created = datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc)
        if created < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    _commit_volume()
    return removed


@app.function(
    image=image,
    secrets=[hub_secret, config_secret],
    volumes={str(STAGING_ROOT): volume},
    timeout=10 * 60,
)
@modal.concurrent(max_inputs=64)
@modal.asgi_app()
def web_app():
    from fastapi import FastAPI, Request, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    api_app = FastAPI(title="LEIN Speech upload API")
    api_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Location", "Upload-Offset", "Upload-Length", "Upload-Defer-Length"],
    )

    def folder_for(upload_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f-]{36}", upload_id):
            raise ValueError("Invalid upload id")
        return UPLOADS_ROOT / upload_id

    def cors_upload_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-store",
            "Tus-Resumable": "1.0.0",
            "Access-Control-Expose-Headers": "Location, Upload-Offset, Upload-Length, Tus-Resumable",
        }
        if extra:
            headers.update(extra)
        return headers

    @api_app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "service": APP_NAME,
            "dataset": os.environ.get("LEIN_DATASET_REPO", ""),
            "max_file_size": MAX_FILE_SIZE,
            "chunk_size": CHUNK_SIZE,
        })

    @api_app.post("/uploads")
    async def create_upload(request: Request) -> Response:
        try:
            length = int(request.headers.get("upload-length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_FILE_SIZE:
            return JSONResponse({"error": "Invalid or missing Upload-Length"}, status_code=400)

        meta = _parse_upload_metadata(request.headers.get("upload-metadata"))
        station = (meta.get("station") or meta.get("station_name") or "").strip()
        filename = (meta.get("filename") or meta.get("name") or "").strip()
        if not station or not filename:
            return JSONResponse({"error": "Station and filename metadata are required"}, status_code=400)
        if len(station) > 120 or len(filename) > 255:
            return JSONResponse({"error": "Station or filename is too long"}, status_code=400)

        safe_name = _safe_filename(filename)
        extension = Path(safe_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            return JSONResponse({"error": f"Unsupported file type: {extension}"}, status_code=400)

        original_relative = (meta.get("relativepath") or meta.get("relative_path") or filename).strip()
        relative_path = _safe_relative_path(original_relative, filename)

        upload_id = str(uuid.uuid4())
        candidate_submission = (meta.get("submissionid") or meta.get("submission_id") or "").strip()
        submission_id = (
            candidate_submission
            if re.fullmatch(r"[A-Za-z0-9_-]{8,120}", candidate_submission)
            else upload_id
        )
        record = {
            "upload_id": upload_id,
            "submission_id": submission_id,
            "length": length,
            "original_filename": filename[:255],
            "original_relative_path": original_relative[:MAX_META_VALUE],
            "safe_filename": safe_name,
            "relative_path": relative_path,
            "content_type": (meta.get("filetype") or meta.get("type") or "application/octet-stream")[:100],
            "station": station[:120],
            "station_slug": _slug(station),
            "email": (meta.get("email") or "")[:120],
            "language": (meta.get("language") or "Unspecified")[:120],
            "created_at": _utc_now(),
        }
        _reload_volume()
        folder = folder_for(upload_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "data.bin").touch()
        _write_json(folder / "meta.json", record)
        _commit_volume()

        record_submission.spawn({
            "submission_id": submission_id,
            "station": station[:120],
            "station_slug": record["station_slug"],
            "email": record["email"],
            "language": record["language"],
        })

        base = str(request.base_url).rstrip("/")
        if base.startswith("http://"):
            base = "https://" + base[len("http://"):]
        location = f"{base}/uploads/{upload_id}"
        return Response(
            status_code=201,
            headers=cors_upload_headers({
                "Location": location,
                "Upload-Offset": "0",
                "Upload-Length": str(length),
            }),
        )

    @api_app.head("/uploads/{upload_id}", name="upload_head")
    async def upload_head(upload_id: str, request: Request) -> Response:
        try:
            folder = folder_for(upload_id)
        except ValueError:
            return Response(status_code=404)
        _reload_volume()
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            return Response(status_code=404)
        meta = _read_json(meta_path)
        data_path = folder / "data.bin"
        offset = data_path.stat().st_size if data_path.exists() else 0
        if (folder / "done.json").exists():
            offset = int(meta["length"])
        return Response(
            status_code=200,
            headers=cors_upload_headers({
                "Upload-Offset": str(offset),
                "Upload-Length": str(meta["length"]),
            }),
        )

    @api_app.patch("/uploads/{upload_id}")
    async def upload_chunk(upload_id: str, request: Request) -> Response:
        try:
            folder = folder_for(upload_id)
        except ValueError:
            return Response(status_code=404)
        _reload_volume()
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            return Response(status_code=404)
        meta = _read_json(meta_path)
        if (folder / "done.json").exists():
            return Response(
                status_code=204,
                headers=cors_upload_headers({"Upload-Offset": str(meta["length"])}),
            )

        try:
            client_offset = int(request.headers.get("upload-offset", "-1"))
        except ValueError:
            client_offset = -1
        data_path = folder / "data.bin"
        server_offset = data_path.stat().st_size if data_path.exists() else 0
        if client_offset != server_offset:
            return Response(
                status_code=409,
                headers=cors_upload_headers({"Upload-Offset": str(server_offset)}),
            )

        chunk = await request.body()
        if not chunk:
            return Response(
                status_code=204,
                headers=cors_upload_headers({"Upload-Offset": str(server_offset)}),
            )
        if server_offset + len(chunk) > int(meta["length"]):
            return JSONResponse({"error": "Chunk exceeds declared Upload-Length"}, status_code=413)

        with data_path.open("ab") as handle:
            handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        new_offset = data_path.stat().st_size
        _commit_volume()

        if new_offset == int(meta["length"]):
            finalize_upload.spawn(upload_id)

        return Response(
            status_code=204,
            headers=cors_upload_headers({"Upload-Offset": str(new_offset)}),
        )

    @api_app.get("/uploads/{upload_id}/status")
    async def upload_status(upload_id: str, request: Request) -> Response:
        try:
            folder = folder_for(upload_id)
        except ValueError:
            return JSONResponse({"error": "Invalid upload id"}, status_code=400)
        _reload_volume()
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            return JSONResponse({"error": "Upload not found"}, status_code=404)
        meta = _read_json(meta_path)
        data_path = folder / "data.bin"
        offset = data_path.stat().st_size if data_path.exists() else 0
        done_path = folder / "done.json"
        if done_path.exists():
            try:
                payload = _read_json(done_path)
                offset = int(meta["length"])
            except Exception:
                payload = {"status": "processing"}
                offset = int(meta["length"])
        elif offset >= int(meta["length"]):
            payload = {"status": "processing"}
        else:
            payload = {"status": "uploading"}
        payload.update({"offset": offset, "length": int(meta["length"])})
        return JSONResponse(payload)

    @api_app.post("/uploads/{upload_id}/retry")
    async def retry_upload(upload_id: str, request: Request) -> Response:
        try:
            folder = folder_for(upload_id)
        except ValueError:
            return JSONResponse({"error": "Invalid upload id"}, status_code=400)
        _reload_volume()
        done_path = folder / "done.json"
        try:
            state = _read_json(done_path) if done_path.exists() else {}
        except Exception:
            state = {}
        if state.get("status") != "failed":
            return JSONResponse({"error": "Only failed uploads can be retried"}, status_code=400)
        done_path.unlink()
        _commit_volume()
        finalize_upload.spawn(upload_id)
        return JSONResponse({"status": "processing"})

    @api_app.delete("/uploads/{upload_id}")
    async def cancel_upload(upload_id: str, request: Request) -> Response:
        try:
            folder = folder_for(upload_id)
        except ValueError:
            return Response(status_code=404)
        _reload_volume()
        shutil.rmtree(folder, ignore_errors=True)
        _commit_volume()
        return Response(status_code=204, headers=cors_upload_headers())

    return api_app
