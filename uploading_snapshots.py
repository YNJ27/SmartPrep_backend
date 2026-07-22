import asyncio
import argparse
import cloudinary
import cloudinary.uploader
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from config import SEMAPHORE_LIMIT_CLOUDINARY
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

SUBJECTS_DIR = Path("subjects")

semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT_CLOUDINARY)
executor = ThreadPoolExecutor(max_workers=SEMAPHORE_LIMIT_CLOUDINARY)


def is_image_file(filename: str) -> bool:
    return filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))


def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "subject"


def _safe_public_id_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("_") or "item"


async def upload_single(file_path: str, public_id: str, question_id: str) -> dict:
    async with semaphore:
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                executor,
                lambda: cloudinary.uploader.upload(file_path, public_id=public_id)
            )
        except Exception as e:
            err_str = str(e).lower()
            if any(keyword in err_str for keyword in ("connection", "timeout", "network", "unreachable", "refused", "resolve")):
                raise RuntimeError(f"NETWORK_ERROR: Failed to connect to Cloudinary uploading '{question_id}': {e}")
            raise RuntimeError(f"Cloudinary upload failed for '{question_id}': {e}")

        secure_url = response.get("secure_url")
        if not secure_url:
            raise RuntimeError(f"Cloudinary returned no secure_url for '{question_id}'")

        return {
            "question_id": question_id,
            "cloud_url": secure_url,
        }


async def upload_paper_snapshots(subject_name: str, paper_name: str) -> list[str]:
    """Upload snapshots for a paper. Returns a list of error strings (empty if all OK)."""
    subject_folder = _safe_folder_name(subject_name)
    paper_folder = _safe_folder_name(paper_name)
    html_dir = SUBJECTS_DIR / subject_folder / "papers" / paper_folder / "html"
    snapshots_dir = html_dir / "question_snapshots"

    if not snapshots_dir.is_dir():
        return []

    tasks = []
    try:
        filenames = sorted(os.listdir(snapshots_dir))
    except OSError as e:
        print(f"[ERROR] Failed to list snapshots in '{snapshots_dir}': {e}", file=sys.stderr)
        return [str(e)]

    for filename in filenames:
        if not is_image_file(filename):
            continue

        file_path = snapshots_dir / filename
        question_no = os.path.splitext(filename)[0]
        question_id = f"{paper_folder}_{_safe_public_id_part(question_no)}"
        public_id = f"{_safe_public_id_part(subject_folder)}_{question_id}"
        tasks.append(upload_single(str(file_path), public_id, question_id))

    if not tasks:
        return []

    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = []
    entries = []
    has_network_error = False

    for result in results:
        if isinstance(result, Exception):
            err_str = str(result)
            errors.append(err_str)
            print(f"[ERROR] Upload failed: {err_str}", file=sys.stderr)
            if "NETWORK_ERROR" in err_str:
                has_network_error = True
        else:
            entries.append(result)

    # Write successfully uploaded snapshots even if some failed
    if entries:
        output_path = html_dir / "snapshots_url.json"
        try:
            with open(output_path, "w", encoding="utf-8") as file:
                json.dump(list(entries), file, indent=2)
        except OSError as e:
            err_str = f"Failed to write snapshots_url.json for '{paper_name}': {e}"
            print(f"[ERROR] {err_str}", file=sys.stderr)
            errors.append(err_str)

    if has_network_error:
        errors.insert(0, "NETWORK_ERROR_FLAG")

    return errors


async def main(subject_name: str):
    subject_folder = _safe_folder_name(subject_name)
    papers_root = SUBJECTS_DIR / subject_folder / "papers"

    if not papers_root.is_dir():
        print(f"[FATAL] papers folder not found for subject '{subject_name}': {papers_root}", file=sys.stderr)
        sys.exit(1)

    try:
        paper_names = sorted(
            name for name in os.listdir(papers_root)
            if os.path.isdir(papers_root / name)
        )
    except OSError as e:
        print(f"[FATAL] Failed to list papers in '{papers_root}': {e}", file=sys.stderr)
        sys.exit(1)

    results = await asyncio.gather(
        *[upload_paper_snapshots(subject_folder, paper) for paper in paper_names],
        return_exceptions=True
    )

    has_network_error = False
    any_failed = False

    for paper_name, result in zip(paper_names, results):
        if isinstance(result, Exception):
            print(f"[ERROR] Unexpected error uploading snapshots for '{paper_name}': {result}", file=sys.stderr)
            any_failed = True
        elif isinstance(result, list) and result:
            any_failed = True
            for err in result:
                if "NETWORK_ERROR_FLAG" in err:
                    has_network_error = True

    if any_failed:
        if has_network_error:
            sys.exit(3)  # EXIT_NETWORK
        else:
            sys.exit(1)  # EXIT_LOGICAL


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload snapshots to Cloudinary for one subject")
    parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
    args = parser.parse_args()

    asyncio.run(main(args.subject))