import json
import os
import re
import sys
import argparse
from pathlib import Path
from typing import Any

import cloudinary
import cloudinary.uploader
from cloudinary.utils import cloudinary_url
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)


def _require_env() -> None:
    missing = [
        name
        for name in (
            "CLOUDINARY_CLOUD_NAME",
            "CLOUDINARY_API_KEY",
            "CLOUDINARY_API_SECRET",
        )
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"Missing Cloudinary env vars: {', '.join(missing)}")


def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "subject"


def _safe_public_id_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("_") or "item"


def upload_base64_to_cloudinary(data_uri: str, public_id: str) -> str:
    try:
        result = cloudinary.uploader.upload(
            data_uri,
            public_id=public_id,
            overwrite=True,
            resource_type="image",
        )
    except Exception as e:
        err_str = str(e).lower()
        if any(keyword in err_str for keyword in ("connection", "timeout", "network", "unreachable", "refused", "resolve")):
            raise RuntimeError(f"NETWORK_ERROR: Failed to connect to Cloudinary for image '{public_id}': {e}")
        raise RuntimeError(f"Cloudinary upload failed for image '{public_id}': {e}")

    try:
        url, _ = cloudinary_url(
            result["public_id"],
            fetch_format="webp",
            quality="100",
            secure=True,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to build Cloudinary URL for '{public_id}': {e}")

    return url


def atomic_write_json(target_path: Path, data: Any) -> None:
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, ensure_ascii=False, indent=2)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, target_path)
    except OSError as e:
        # Clean up temp file if it exists
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to write JSON to '{target_path}': {e}")


def process_paper_json(paper_json_path: Path, subject_name: str, paper_name: str) -> bool:
    try:
        with paper_json_path.open("r", encoding="utf-8") as json_file:
            data = json.load(json_file)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Failed to read or parse '{paper_json_path}': {e}")

    pages = data.get("pages", [])
    if not isinstance(pages, list):
        raise ValueError(f"Invalid pages structure in {paper_json_path}")

    upload_plan = []
    for page in pages:
        images = page.get("images", [])
        if not images:
            continue
        for image in images:
            image_id = image.get("id")
            image_base64 = image.get("image_base64")
            if not image_id or not image_base64:
                raise ValueError(
                    f"Missing image data in {paper_json_path} for page index {page.get('index')}"
                )
            image_stem = Path(image_id).stem
            public_id = (
                f"{_safe_public_id_part(subject_name)}_"
                f"{_safe_public_id_part(paper_name)}_"
                f"{_safe_public_id_part(image_stem)}"
            )
            upload_plan.append((image, image_base64, public_id))

    for image, image_base64, public_id in upload_plan:
        image["cloud_url"] = upload_base64_to_cloudinary(image_base64, public_id)

    atomic_write_json(paper_json_path, data)
    return True


def main(subject_name: str) -> None:
    try:
        _require_env()
    except RuntimeError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)

    subject_folder = _safe_folder_name(subject_name)
    papers_root = Path("subjects") / subject_folder / "papers"

    if not papers_root.exists():
        print(f"[FATAL] papers folder not found: {papers_root}", file=sys.stderr)
        sys.exit(1)

    try:
        paper_dirs = sorted(p for p in papers_root.iterdir() if p.is_dir())
    except OSError as e:
        print(f"[FATAL] Failed to list paper directories in '{papers_root}': {e}", file=sys.stderr)
        sys.exit(1)

    if not paper_dirs:
        print("No paper folders found")
        return

    failures = []
    processed = 0
    has_network_error = False

    for paper_dir in paper_dirs:
        paper_name = paper_dir.name
        paper_json_path = paper_dir / "ocr" / "paper.json"
        if not paper_json_path.exists():
            print(f"Skipping {paper_name}: paper.json not found")
            continue

        try:
            process_paper_json(paper_json_path, subject_folder, paper_name)
            processed += 1
            print(f"Updated {paper_json_path}")
        except RuntimeError as exc:
            err_str = str(exc)
            failures.append((paper_name, err_str))
            print(f"[ERROR] Failed {paper_name}: {err_str}", file=sys.stderr)
            if "NETWORK_ERROR" in err_str:
                has_network_error = True
        except Exception as exc:
            failures.append((paper_name, str(exc)))
            print(f"[ERROR] Failed {paper_name}: {exc}", file=sys.stderr)

    print(f"Processed: {processed}")
    if failures:
        print("[ERROR] Failures:", file=sys.stderr)
        for paper_name, error in failures:
            print(f"  - {paper_name}: {error}", file=sys.stderr)

    if failures:
        if has_network_error:
            sys.exit(3)  # EXIT_NETWORK
        else:
            sys.exit(1)  # EXIT_LOGICAL


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload paper images to Cloudinary for one subject")
    parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
    args = parser.parse_args()

    main(args.subject)
