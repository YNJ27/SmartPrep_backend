from __future__ import annotations

import os
import re
import sys
import subprocess
import json
import hashlib
import uuid
import shutil
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from urllib.request import Request, urlopen

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Response, Request, Cookie, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client, Client
from config import LOCK_STALE_THRESHOLD_SECONDS
BASE_DIR = Path(__file__).resolve().parent
SUBJECTS_DIR = BASE_DIR / "subjects"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Warning: Missing Supabase credentials in .env")

supabase_admin: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN") or None
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SessionPayload(BaseModel):
    access_token: str
    refresh_token: str

def _set_session_cookies(response: Response, access_token: str, refresh_token: str, expires_in: int) -> None:
    response.set_cookie(
        "access_token", access_token, httponly=True, secure=COOKIE_SECURE,
        samesite="none", max_age=expires_in, path="/",
        domain=COOKIE_DOMAIN,   # <-- add this
    )
    response.set_cookie(
        "refresh_token", refresh_token, httponly=True, secure=COOKIE_SECURE,
        samesite="none", max_age=60 * 60 * 24 * 30, path="/",
        domain=COOKIE_DOMAIN,   # <-- add this
    )

@app.post("/auth/session")
def create_session_from_tokens(payload: SessionPayload, response: Response):
    """Frontend hands off a session obtained via supabase-js. We verify the
    access_token is genuinely valid before trusting it, then store both
    tokens as httpOnly cookies. This is the ONLY place tokens cross from
    JS-visible memory into the browser at all, and only as httpOnly cookies
    JS cannot read."""
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    try:
        res = supabase_admin.auth.get_user(payload.access_token)
        if not res or not res.user:
            raise HTTPException(status_code=401, detail="Invalid session token")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token verification failed: {e}")

    _set_session_cookies(response, payload.access_token, payload.refresh_token, expires_in=3600)
    return {"message": "Session established", "user_id": res.user.id}

async def get_current_user(
    response: Response,
    access_token: str | None = Cookie(default=None),
    refresh_token: str | None = Cookie(default=None),
):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        res = supabase_admin.auth.get_user(access_token)
        if res and res.user:
            return res.user
    except Exception:
        pass  # fall through to refresh attempt

    if not refresh_token:
        raise HTTPException(status_code=401, detail="Session expired")

    try:
        refreshed = supabase_admin.auth.refresh_session(refresh_token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Session expired: {e}")

    if not refreshed or not refreshed.session:
        raise HTTPException(status_code=401, detail="Session expired")

    _set_session_cookies(
        response,
        refreshed.session.access_token,
        refreshed.session.refresh_token,
        expires_in=refreshed.session.expires_in or 3600,
    )
    return refreshed.session.user

@app.get("/auth/me")
def get_me(current_user = Depends(get_current_user)):
    return {"user_id": current_user.id, "email": current_user.email}

@app.post("/auth/logout")
def logout(response: Response):
    if supabase_admin:
        try:
            supabase_admin.auth.sign_out()
        except Exception:
            pass
    response.delete_cookie("access_token", path="/", domain=COOKIE_DOMAIN)
    response.delete_cookie("refresh_token", path="/", domain=COOKIE_DOMAIN)
    return {"message": "Logged out successfully"}

class FileItem(BaseModel):
    id: str
    name: str
    downloadUrl: str = Field(..., alias="downloadUrl")

class MetadataItem(BaseModel):
    Branch: str
    Year: str
    Pattern: str

class SubjectPayload(BaseModel):
    subject: str
    examType: str
    metadata: MetadataItem
    files: list[FileItem]

def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "subject"

def compute_file_hash(files: list[FileItem]) -> str:
    sorted_ids = sorted([f.id for f in files])
    hash_str = "".join(sorted_ids)
    return hashlib.sha256(hash_str.encode("utf-8")).hexdigest()

def _download_to_path(url: str, destination: Path) -> int:
    import urllib.error
    import urllib.request
    default_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36 Brave/125"
    )
    ua = os.getenv("DOWNLOAD_USER_AGENT", default_ua)
    headers = {"User-Agent": ua}
    request = urllib.request.Request(url, headers=headers)

    try:
        with urlopen(request, timeout=60) as response, destination.open("wb") as file_handle:
            total_bytes = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                file_handle.write(chunk)
                total_bytes += len(chunk)
    except urllib.error.URLError as e:
        raise ConnectionError(f"Network error downloading file: {e.reason}")
    except TimeoutError as e:
        raise ConnectionError(f"Download timed out: {e}")
    except OSError as e:
        raise ConnectionError(f"Failed to write downloaded file: {e}")

    return total_bytes

# Orchestrator exit codes (must match orchestrator.py EXIT_* constants)
_EXIT_RATE_LIMIT = 2
_EXIT_NETWORK = 3

# User-facing error messages stored in DB (shown to frontend)
_ERR_RATE_LIMIT = "Google Api Key has hit rate limit, please try after 5+ mins or change the Api Key"
_ERR_NETWORK = "A network error occurred while processing the PDFs. Please check your internet connection and try again."
_ERR_GENERIC = "Failed to process the PDFs"


def _run_orchestrator(
    run_id: str, 
    subject: str, 
    exam_type: str, 
    branch: str, 
    year: str, 
    pattern: str, 
    file_hash: str,
    user_id: str
) -> None:
    orchestrator_path = BASE_DIR / "orchestrator.py"
    run_folder = f"_runs_{run_id}"
    
    try:
        print(f"[Background Task] Running orchestrator for run_id: {run_id}")
        # stdout/stderr are inherited — all child output streams live to the backend terminal.
        # We check the exit code to determine the error type (no pipe needed).
        result = subprocess.run(
            [sys.executable, str(orchestrator_path), run_folder, exam_type, user_id],
            check=False,
            cwd=str(BASE_DIR)
        )
        returncode = result.returncode

        if returncode != 0:
            # Map exit code to the appropriate frontend-facing error message
            if returncode == _EXIT_RATE_LIMIT:
                frontend_error = _ERR_RATE_LIMIT
                print(f"[Background Task] Google API rate limit hit for run_id {run_id}", file=sys.stderr)
            elif returncode == _EXIT_NETWORK:
                frontend_error = _ERR_NETWORK
                print(f"[Background Task] Network error during orchestration for run_id {run_id}", file=sys.stderr)
            else:
                frontend_error = _ERR_GENERIC
                print(f"[Background Task] Orchestrator failed with exit code {returncode} for run_id {run_id}", file=sys.stderr)

            if supabase_admin:
                try:
                    supabase_admin.table("processed_subjects").update({
                        "status": "failed",
                        "error_message": frontend_error
                    }).eq("file_hash", file_hash).eq("run_id", run_id).execute()
                except Exception as db_err:
                    print(f"[Background Task] Warning: failed to update failure status in DB: {db_err}", file=sys.stderr)
            return

        # --- Success path ---
        grouped_dir = SUBJECTS_DIR / run_folder / "grouped_questions"
        storage_prefix = f"{_safe_folder_name(branch)}/{_safe_folder_name(year)}/{_safe_folder_name(pattern)}/{_safe_folder_name(subject)}/{_safe_folder_name(exam_type)}"

        if grouped_dir.exists() and supabase_admin:
            for json_file in grouped_dir.glob("*.json"):
                try:
                    with json_file.open("rb") as f:
                        file_bytes = f.read()
                    storage_path = f"{storage_prefix}/{json_file.name}"
                    # upsert=True replaces if exists
                    supabase_admin.storage.from_("grouped-questions").upload(
                        file=file_bytes,
                        path=storage_path,
                        file_options={"cacheControl": "3600", "upsert": "true", "contentType": "application/json"}
                    )
                except Exception as upload_err:
                    print(f"[Background Task] Warning: failed to upload '{json_file.name}' to storage: {upload_err}", file=sys.stderr)

        # Mark as completed in DB (clear any stale error_message from a prior failed run)
        now_iso = datetime.now(timezone.utc).isoformat()
        if supabase_admin:
            try:
                supabase_admin.table("processed_subjects").update({
                    "status": "completed",
                    "storage_path": storage_prefix,
                    "processed_at": now_iso,
                    "error_message": None
                }).eq("file_hash", file_hash).eq("run_id", run_id).execute()
            except Exception as db_err:
                print(f"[Background Task] Warning: failed to mark run as completed in DB: {db_err}", file=sys.stderr)

        print(f"[Background Task] Orchestrator completed successfully for run_id: {run_id}")

    except Exception as e:
        # Catch-all for unexpected errors (e.g. subprocess itself failing to launch)
        print(f"[Background Task] Unexpected error running orchestrator for run_id {run_id}: {e}", file=sys.stderr)
        if supabase_admin:
            try:
                supabase_admin.table("processed_subjects").update({
                    "status": "failed",
                    "error_message": _ERR_GENERIC
                }).eq("file_hash", file_hash).eq("run_id", run_id).execute()
            except Exception as db_err:
                print(f"[Background Task] Warning: failed to update failure status in DB: {db_err}", file=sys.stderr)

    finally:
        # Cleanup temp folder
        temp_dir = SUBJECTS_DIR / run_folder
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
                print(f"[Background Task] Cleaned up temporary folder {temp_dir}")
            except Exception as e:
                print(f"[Background Task] Failed to clean up {temp_dir}: {e}", file=sys.stderr)


def _cleanup_stale_run_folder(run_id: str | None) -> None:
    """Delete the _runs_<run_id> folder inside SUBJECTS_DIR if it exists."""
    if not run_id:
        return
    stale_dir = SUBJECTS_DIR / f"_runs_{run_id}"
    if stale_dir.exists():
        try:
            shutil.rmtree(stale_dir)
            print(f"[Cleanup] Removed stale run folder: {stale_dir}")
        except Exception as e:
            print(f"[Cleanup] Failed to remove stale run folder {stale_dir}: {e}", file=sys.stderr)




@app.post("/subjects/import-pdfs")
def import_subject_pdfs(
    payload: SubjectPayload,
    background_tasks: BackgroundTasks,
    current_user = Depends(get_current_user)
) -> dict[str, Any]:
    file_hash = compute_file_hash(payload.files)
    
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    # 1. Fast-path: check for an already-completed or actively-pending record.
    res = supabase_admin.table("processed_subjects").select("*").eq("file_hash", file_hash).execute()

    if len(res.data) > 0:
        row = res.data[0]
        status = row.get("status")
        if status == "completed":
            return {
                "message": "Already processed.",
                "subject": payload.subject,
                "examType": payload.examType,
                "status": "completed",
                "storage_path": row.get("storage_path"),
                "file_hash": file_hash
            }
        elif status == "failed":
            # Clean up any leftover run folder from the failed attempt
            _cleanup_stale_run_folder(row.get("run_id"))

        elif status == "pending":
            lock_time_str = row.get("lock_timestamp")
            if lock_time_str:
                try:
                    lock_dt = datetime.fromisoformat(lock_time_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
                    now_utc = datetime.now(timezone.utc)
                    delta = now_utc - lock_dt

                    if delta.total_seconds() < LOCK_STALE_THRESHOLD_SECONDS:
                        return {
                            "message": "Currently processing in background.",
                            "subject": payload.subject,
                            "examType": payload.examType,
                            "status": "processing",
                            "file_hash": file_hash
                        }
                    else:
                        # Lock is stale — clean up the orphaned run folder
                        print(f"[Import] Stale lock detected for file_hash={file_hash}. Cleaning up old run folder.")
                        _cleanup_stale_run_folder(row.get("run_id"))
                        try:
                            supabase_admin.table("processed_subjects").update({
                                "status": "failed",
                                "error_message": "Previous processing attempt timed out."
                            }).eq("file_hash", file_hash).execute()
                        except Exception as db_err:
                            print(f"[Import] Warning: failed to mark stale lock as failed in DB: {db_err}", file=sys.stderr)

                except Exception as e:
                    print(f"Error parsing date {lock_time_str}: {e}")

    # 2. Atomically acquire the lock via INSERT … ON CONFLICT DO NOTHING.
    #
    #    Two concurrent requests for the same file_hash can both pass the
    #    fast-path check above (the row doesn't exist yet for either of them).
    #    By using ignore_duplicates=True the DB guarantees that only ONE of
    #    those inserts actually lands; the other is silently dropped.
    #    We then re-read the row: if the run_id stored in the DB is NOT the one
    #    we just generated, we lost the race and must return "processing".
    #
    #    For the "failed" case the fast-path already removed the old row via
    #    _cleanup_stale_run_folder, so the insert below will succeed cleanly.
    #    (If the row was NOT deleted—e.g. only the folder was cleaned—we will
    #    lose the insert but then detect the stale/failed status in step 3 and
    #    fall through to a proper upsert that resets the record.)

    now_iso = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())

    try:
        supabase_admin.table("processed_subjects").insert({
            "subject": payload.subject,
            "exam_type": payload.examType,
            "branch": payload.metadata.Branch,
            "year": payload.metadata.Year,
            "pattern": payload.metadata.Pattern,
            "file_hash": file_hash,
            "file_count": len(payload.files),
            "status": "pending",
            "error_message": None,
            "lock_timestamp": now_iso,
            "run_id": run_id
        }, returning="minimal").execute()
    except Exception:
        # A conflict (or other DB error) means either another request already
        # inserted the row, or we need to overwrite a stale/failed record.
        # Fall through to step 3 to handle both cases.
        pass

    # 3. Re-read the authoritative row from the DB.
    #    This is the source of truth regardless of what happened above.
    try:
        verify_res = supabase_admin.table("processed_subjects").select("*").eq("file_hash", file_hash).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to verify lock: {str(e)}")

    if not verify_res.data:
        # This should only happen on DB errors; treat as a lock failure.
        raise HTTPException(status_code=500, detail="Failed to acquire processing lock.")

    db_row = verify_res.data[0]
    db_status = db_row.get("status")
    db_run_id = db_row.get("run_id")

    if db_status == "completed":
        # Another request completed processing between our fast-path check and now.
        return {
            "message": "Already processed.",
            "subject": payload.subject,
            "examType": payload.examType,
            "status": "completed",
            "storage_path": db_row.get("storage_path"),
            "file_hash": file_hash
        }

    if db_status == "pending" and db_run_id != run_id:
        # Another concurrent request won the race and holds the lock.
        return {
            "message": "Currently processing in background.",
            "subject": payload.subject,
            "examType": payload.examType,
            "status": "processing",
            "file_hash": file_hash
        }

    if db_status == "failed":
        # The row is a leftover from a previous failed run (folder cleanup was
        # done in the fast-path above).  Reset it with our new run_id so the
        # pipeline can start fresh.
        try:
            supabase_admin.table("processed_subjects").update({
                "subject": payload.subject,
                "exam_type": payload.examType,
                "branch": payload.metadata.Branch,
                "year": payload.metadata.Year,
                "pattern": payload.metadata.Pattern,
                "file_count": len(payload.files),
                "status": "pending",
                "error_message": None,
                "lock_timestamp": now_iso,
                "run_id": run_id
            }).eq("file_hash", file_hash).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to reset failed record: {str(e)}")

    # 4. Setup Temp Folder
    run_folder = f"_runs_{run_id}"
    raw_pdfs_dir = SUBJECTS_DIR / run_folder / "raw_pdfs"
    raw_pdfs_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for file_item in payload.files:
        target_path = raw_pdfs_dir / file_item.name
        try:
            bytes_written = _download_to_path(file_item.downloadUrl, target_path)
            saved_files.append({
                "id": file_item.id,
                "name": file_item.name,
                "path": str(target_path),
                "bytes": bytes_written,
            })
        except ConnectionError as exc:
            print(f"[Import] Network error downloading '{file_item.name}': {exc}", file=sys.stderr)
            if target_path.exists():
                try: target_path.unlink()
                except OSError: pass
            errors.append({"id": file_item.id, "name": file_item.name, "error": "network", "detail": str(exc)})
        except Exception as exc:
            print(f"[Import] Failed to download '{file_item.name}': {exc}", file=sys.stderr)
            if target_path.exists():
                try: target_path.unlink()
                except OSError: pass
            errors.append({"id": file_item.id, "name": file_item.name, "error": str(exc)})

    if errors:
        # Determine user-facing error: if all failures are network errors, say so
        all_network = all(e.get("error") == "network" for e in errors)
        download_error_msg = (
            "A network error occurred while downloading the PDF files. Please check your connection and try again."
            if all_network
            else "Failed to download one or more PDF files. Please try again."
        )

        try:
            supabase_admin.table("processed_subjects").update({
                "status": "failed",
                "error_message": download_error_msg
            }).eq("file_hash", file_hash).execute()
        except Exception as db_err:
            print(f"[Import] Warning: failed to update DB after download failure: {db_err}", file=sys.stderr)
        
        raise HTTPException(
            status_code=500,
            detail={
                "message": download_error_msg,
                "errors": [{"id": e["id"], "name": e["name"]} for e in errors],
            },
        )

    # 4. Schedule the orchestrator execution
    background_tasks.add_task(
        _run_orchestrator, 
        run_id=run_id, 
        subject=payload.subject, 
        exam_type=payload.examType,
        branch=payload.metadata.Branch,
        year=payload.metadata.Year,
        pattern=payload.metadata.Pattern,
        file_hash=file_hash,
        user_id=current_user.id
    )

    return {
        "message": "Files downloaded successfully. Orchestrator started in background.",
        "subject": payload.subject,
        "examType": payload.examType,
        "status": "processing",
        "file_hash": file_hash
    }


@app.get("/subjects/status/{file_hash}")
def get_subject_status(file_hash: str, current_user = Depends(get_current_user)) -> dict[str, Any]:
    """Returns the current processing status from Supabase using the file hash."""
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    try:
        res = supabase_admin.table("processed_subjects").select("*").eq("file_hash", file_hash).execute()
    except Exception as e:
        # Handle network or protocol errors without retrying
        raise HTTPException(status_code=500, detail=f"Failed to fetch status: {str(e)}")
    if not res.data:
        raise HTTPException(status_code=404, detail="Status not found for this file hash.")
    row = res.data[0]
    return {
        "status": row.get("status"),
        "error": row.get("error_message"),
        "storage_path": row.get("storage_path")
    }


@app.get("/subjects/{subject}/grouped-questions")
def get_grouped_questions(
    subject: str,
    exam_type: str = Query(default="", alias="examType"),
    current_user = Depends(get_current_user)
) -> list[dict[str, Any]]:
    """Returns all grouped unit question JSONs for a subject from Supabase Storage.
    
    Args:
        subject:   The subject name (path parameter).
        exam_type: Optional exam type filter, e.g. 'Endsem' or 'Insem' (query parameter).
                   When provided, only records matching that exam type are considered.
    """
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    query = (
        supabase_admin.table("processed_subjects")
        .select("storage_path")
        .eq("subject", subject)
        .eq("status", "completed")
    )

    if exam_type:
        query = query.eq("exam_type", exam_type)

    res = query.order("processed_at", desc=True).limit(1).execute()

    if not res.data:
        raise HTTPException(
            status_code=404,
            detail=f"No completed processing found for subject '{subject}'."
        )
    
    storage_prefix = res.data[0].get("storage_path")
    if not storage_prefix:
        raise HTTPException(status_code=500, detail="Invalid storage path in database.")

    list_res = supabase_admin.storage.from_("grouped-questions").list(storage_prefix)
    
    if not list_res or not isinstance(list_res, list):
        raise HTTPException(
            status_code=404,
            detail=f"No grouped unit files found in storage at '{storage_prefix}'."
        )

    results: list[dict[str, Any]] = []
    
    for file_obj in list_res:
        file_name = file_obj.get("name", "")
        match = re.search(r"(\d+)\.json$", file_name)
        if not match:
            continue
            
        unit_number = int(match.group(1))
        
        file_path = f"{storage_prefix}/{file_name}"
        try:
            res_bytes = supabase_admin.storage.from_("grouped-questions").download(file_path)
            questions = json.loads(res_bytes.decode("utf-8"))
            results.append({"unit": unit_number, "questions": questions})
        except Exception as exc:
            pass

    if not results:
         raise HTTPException(
            status_code=404,
            detail=f"No valid unit JSONs found."
        )

    return results

class UserSubjectPayload(BaseModel):
    subject: str
    examType: str
    Branch: str
    Year: str
    Pattern: str

@app.post("/user/subjects")
def save_user_subject(payload: UserSubjectPayload, current_user = Depends(get_current_user)):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    try:
        # Use upsert to avoid duplicate row errors if the user processes the same subject again
        supabase_admin.table("user_subjects").upsert({
            "user_id": current_user.id,
            "subject": payload.subject,
            "examType": payload.examType,
            "Branch": payload.Branch,
            "Year": payload.Year,
            "Pattern": payload.Pattern
        }).execute()
        return {"message": "Subject saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/user/subjects")
def get_user_subjects(current_user = Depends(get_current_user)):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    res = supabase_admin.table("user_subjects").select("*").eq("user_id", current_user.id).order("added_at", desc=True).execute()
    return res.data

@app.delete("/user/subjects/{subject_id}")
def delete_user_subject(subject_id: str, current_user = Depends(get_current_user)):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    
    subject_res = supabase_admin.table("user_subjects").select("*").eq("id", subject_id).eq("user_id", current_user.id).execute()
    if not subject_res.data:
        raise HTTPException(status_code=404, detail="Subject not found")
        
    subject_row = subject_res.data[0]
    supabase_admin.table("user_subjects").delete().eq("id", subject_id).execute()
    
    match_dict = {
        "user_id": current_user.id,
        "subject": subject_row["subject"],
        "examType": subject_row["examType"],
        "Branch": subject_row["Branch"],
        "Year": subject_row["Year"],
        "Pattern": subject_row["Pattern"]
    }
    
    supabase_admin.table("user_group_status").delete().match(match_dict).execute()
    supabase_admin.table("user_question_progress").delete().match(match_dict).execute()
    return {"message": "Subject and progress deleted"}

@app.get("/user/progress")
def get_user_progress(
    subject: str, examType: str, Branch: str, Year: str, Pattern: str, unit_number: int,
    current_user = Depends(get_current_user)
):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    match_dict = {
        "user_id": current_user.id, "subject": subject, "examType": examType,
        "Branch": Branch, "Year": Year, "Pattern": Pattern, "unit_number": unit_number
    }
    status_res = supabase_admin.table("user_group_status").select("group_id, status").match(match_dict).execute()
    progress_res = supabase_admin.table("user_question_progress").select("group_id, question_id, is_done").match(match_dict).execute()
    return {"group_status": status_res.data, "question_progress": progress_res.data}

class GroupStatusPayload(BaseModel):
    subject: str
    examType: str
    Branch: str
    Year: str
    Pattern: str
    unit_number: int
    group_id: int
    status: str

@app.post("/user/group-status")
def save_group_status(payload: GroupStatusPayload, current_user = Depends(get_current_user)):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    match_dict = {
        "user_id": current_user.id, "subject": payload.subject, "examType": payload.examType,
        "Branch": payload.Branch, "Year": payload.Year, "Pattern": payload.Pattern,
        "unit_number": payload.unit_number, "group_id": payload.group_id
    }
    
    if payload.status == "unattempted":
        supabase_admin.table("user_group_status").delete().match(match_dict).execute()
    else:
        now_iso = datetime.now(timezone.utc).isoformat()
        res = supabase_admin.table("user_group_status").update({"status": payload.status, "updated_at": now_iso}).match(match_dict).execute()
        if not res.data:
            supabase_admin.table("user_group_status").insert({**match_dict, "status": payload.status, "updated_at": now_iso}).execute()
    return {"message": "Group status updated"}

class QuestionProgressPayload(BaseModel):
    subject: str
    examType: str
    Branch: str
    Year: str
    Pattern: str
    unit_number: int
    group_id: int
    question_id: str
    is_done: bool

@app.post("/user/question-progress")
def save_question_progress(payload: QuestionProgressPayload, current_user = Depends(get_current_user)):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    match_dict = {
        "user_id": current_user.id, "subject": payload.subject, "examType": payload.examType,
        "Branch": payload.Branch, "Year": payload.Year, "Pattern": payload.Pattern,
        "unit_number": payload.unit_number, "group_id": payload.group_id, "question_id": payload.question_id
    }
    
    now_iso = datetime.now(timezone.utc).isoformat()
    res = supabase_admin.table("user_question_progress").update({"is_done": payload.is_done, "updated_at": now_iso}).match(match_dict).execute()
    if not res.data:
        supabase_admin.table("user_question_progress").insert({**match_dict, "is_done": payload.is_done, "updated_at": now_iso}).execute()
    return {"message": "Question progress updated"}

class GroupResetPayload(BaseModel):
    subject: str
    examType: str
    Branch: str
    Year: str
    Pattern: str
    unit_number: int
    group_id: int

@app.post("/user/group-reset")
def reset_group_progress(payload: GroupResetPayload, current_user = Depends(get_current_user)):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
        
    match_dict = {
        "user_id": current_user.id, "subject": payload.subject, "examType": payload.examType,
        "Branch": payload.Branch, "Year": payload.Year, "Pattern": payload.Pattern,
        "unit_number": payload.unit_number, "group_id": payload.group_id
    }
    supabase_admin.table("user_question_progress").delete().match(match_dict).execute()
    return {"message": "Group progress reset"}

class APIKeyPayload(BaseModel):
    provider: str
    api_key: str

@app.get("/user/api-keys")
def get_user_api_keys(current_user = Depends(get_current_user)):
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    res = supabase_admin.table("user_api_keys").select("provider").eq("user_id", current_user.id).execute()
    return {"providers": [row["provider"] for row in res.data]}

@app.post("/user/api-keys")
def save_api_key(payload: APIKeyPayload, current_user = Depends(get_current_user)) -> dict[str, Any]:
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    
    try:
        from security.encryption import encrypt_api_key
        encrypted_key = encrypt_api_key(payload.api_key)
        
        # Upsert the key
        supabase_admin.table("user_api_keys").upsert({
            "user_id": current_user.id,
            "provider": payload.provider,
            "encrypted_api_key": encrypted_key
        }, on_conflict="user_id,provider").execute()
        
        return {"message": f"{payload.provider} API key saved successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
