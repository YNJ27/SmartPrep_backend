import json
import sys
from pathlib import Path

import numpy as np
from collections import defaultdict
from sklearn.cluster import AgglomerativeClustering
from config import DISTANCE_THRESHOLD
import argparse
import re

PROJECT_ROOT = Path(__file__).parent


def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "subject"


def subject_paths(subject_name: str):
    sf = _safe_folder_name(subject_name)
    units = PROJECT_ROOT / "subjects" / sf / "temporary_embedddings_storage"
    output = PROJECT_ROOT / "subjects" / sf / "grouped_questions"
    papers = PROJECT_ROOT / "subjects" / sf / "papers"
    return units, output, papers

_snapshots_cache: dict[str, dict[str, str]] = {}


def _paper_from_question_id(question_id: str) -> str | None:
    if "_Q" not in question_id:
        return None
    return question_id.split("_Q", 1)[0]


def _load_snapshots_map(paper_name: str, papers_dir: Path) -> dict[str, str]:
    if paper_name in _snapshots_cache:
        return _snapshots_cache[paper_name]

    snapshots_path = papers_dir / paper_name / "html" / "snapshots_url.json"
    if not snapshots_path.exists():
        _snapshots_cache[paper_name] = {}
        return _snapshots_cache[paper_name]

    try:
        with snapshots_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARNING] Failed to read snapshots map for '{paper_name}': {e}", file=sys.stderr)
        _snapshots_cache[paper_name] = {}
        return _snapshots_cache[paper_name]

    snapshots_map = {
        entry["question_id"]: entry.get("cloud_url")
        for entry in data
        if isinstance(entry, dict) and "question_id" in entry
    }
    _snapshots_cache[paper_name] = snapshots_map
    return snapshots_map


def _get_snapshot_url(question_id: str, papers_dir: Path) -> str | None:
    paper_name = _paper_from_question_id(question_id)
    if not paper_name:
        return None
    return _load_snapshots_map(paper_name, papers_dir).get(question_id)


def _cluster_unit(unit_path: Path, papers_dir: Path) -> list[dict]:
    try:
        with unit_path.open("r", encoding="utf-8") as unit_file:
            unit_data = json.load(unit_file)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Failed to read or parse unit file '{unit_path}': {e}")

    if not unit_data:
        print(f"[WARNING] Unit file '{unit_path}' is empty, skipping.")
        return []

    try:
        embeddings = np.array([obj["embedding"] for obj in unit_data])
    except (KeyError, ValueError) as e:
        raise RuntimeError(f"Invalid embedding data in '{unit_path}': {e}")

    question_ids = [obj["question_id"] for obj in unit_data]
    qid_to_marks = {obj["question_id"]: obj.get("marks") for obj in unit_data}

    # No need for normalization since Gemini embeddings are already normalized.
    try:
        clustering = AgglomerativeClustering(
            n_clusters=None,
            metric="cosine",
            linkage="complete",
            distance_threshold=DISTANCE_THRESHOLD,
        )
        labels = clustering.fit_predict(embeddings)
    except Exception as e:
        raise RuntimeError(f"Clustering failed for unit '{unit_path.name}': {e}")

    groups = defaultdict(list)
    for qid, label in zip(question_ids, labels):
        groups[label].append(qid)

    sorted_groups = sorted(groups.values(), key=len, reverse=True)

    grouped_payload = []
    for group_id, group in enumerate(sorted_groups, 1):
        questions = [
            {
                "question_id": qid,
                "snapshot_url": _get_snapshot_url(qid, papers_dir),
                "marks": qid_to_marks.get(qid),
            }
            for qid in group
        ]
        grouped_payload.append(
            {
                "group_id": group_id,
                "size": len(group),
                "questions": questions,
            }
        )

    return grouped_payload


def main(subject_name: str) -> None:
    units_dir, output_dir, papers_dir = subject_paths(subject_name)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[FATAL] Failed to create output directory '{output_dir}': {e}", file=sys.stderr)
        sys.exit(1)

    try:
        unit_paths = sorted(units_dir.glob("unit_*.json"))
    except OSError as e:
        print(f"[FATAL] Failed to list unit files in '{units_dir}': {e}", file=sys.stderr)
        sys.exit(1)

    if not unit_paths:
        print(f"[FATAL] No unit embedding files found in '{units_dir}'", file=sys.stderr)
        sys.exit(1)

    any_failed = False

    for unit_path in unit_paths:
        unit_number = unit_path.stem.split("_")[-1]
        try:
            grouped_data = _cluster_unit(unit_path, papers_dir)
        except RuntimeError as e:
            print(f"[ERROR] Failed to cluster unit '{unit_path.name}': {e}", file=sys.stderr)
            any_failed = True
            continue
        except Exception as e:
            print(f"[ERROR] Unexpected error clustering unit '{unit_path.name}': {e}", file=sys.stderr)
            any_failed = True
            continue

        output_path = output_dir / f"grouped_unit_{unit_number}.json"
        try:
            with output_path.open("w", encoding="utf-8") as out_file:
                json.dump(grouped_data, out_file, ensure_ascii=False, indent=2)
            print(f"Wrote grouped questions to {output_path}")
        except OSError as e:
            print(f"[ERROR] Failed to write grouped output to '{output_path}': {e}", file=sys.stderr)
            any_failed = True

    if any_failed:
        print("[FATAL] One or more units failed during clustering.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cluster unit embeddings for a subject")
    parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
    args = parser.parse_args()

    main(args.subject)