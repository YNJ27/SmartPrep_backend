import asyncio
import aiohttp
import json
import sys
from collections import deque
from pathlib import Path
import os
import argparse
import re
from config import SEMAPHORE_LIMIT, RATE_LIMIT, DEFAULT_EXAM_TYPE #("Endsem" or "Insem")
from dotenv import load_dotenv
from security.encryption import decrypt_api_key
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
except Exception as e:
    print(f"[ERROR] Failed to initialize Supabase client: {e}", file=sys.stderr)
    supabase = None

EMBEDDING_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent"
GOOGLE_API_KEY = None

def fetch_google_api_key(user_id: str) -> str:
    if not supabase:
        raise Exception("Supabase client not initialized")
    try:
        res = supabase.table("user_api_keys").select("encrypted_api_key").eq("user_id", user_id).eq("provider", "Google").execute()
    except Exception as e:
        raise Exception(f"Network error while fetching Google API key: {e}")
    if not res.data:
        raise Exception(f"No Google API key found for user {user_id}")
    encrypted_key = res.data[0]["encrypted_api_key"]
    try:
        return decrypt_api_key(encrypted_key)
    except Exception as e:
        raise Exception(f"Failed to decrypt Google API key: {e}")


semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
request_times = deque()
rate_lock = asyncio.Lock()


def discover_input_files(papers_root: Path) -> list[Path]:
	return sorted(papers_root.glob("*/questions/subquestions.json"))


def _safe_folder_name(name: str) -> str:
	cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name.strip())
	cleaned = re.sub(r"\s+", " ", cleaned)
	return cleaned or "subject"


def load_records_from_json(path: Path) -> list[dict]:
	try:
		with path.open("r", encoding="utf-8") as f:
			data = json.load(f)
	except (OSError, json.JSONDecodeError) as e:
		raise RuntimeError(f"Failed to read or parse JSON file '{path}': {e}")

	if isinstance(data, list):
		return data

	if isinstance(data, dict):
		if "subquestions" in data and isinstance(data["subquestions"], list):
			return data["subquestions"]
		if "questions" in data and isinstance(data["questions"], list):
			return data["questions"]

	raise ValueError(f"Unsupported JSON structure in {path}")


def normalize_record(record: dict, source_file: Path, index: int) -> dict:
	if "question_id" not in record:
		raise ValueError(f"Missing 'question_id' field in record {index} from {source_file}")
	if "text" not in record:
		raise ValueError(f"Missing 'text' field in record {record.get('question_id')} from {source_file}")
	return record


def parse_question_number(question_id: str) -> int:
	if "_Q" not in question_id:
		raise ValueError(f"Invalid question_id format: {question_id}")
	after_q = question_id.split("_Q", 1)[1]
	number_part = after_q.split("_", 1)[0]
	return int(number_part)


def get_unit_name(exam_type: str, question_id: str) -> str:
	question_number = parse_question_number(question_id)

	if exam_type == "Endsem":
		if question_number in (1, 2):
			return "unit_3"
		if question_number in (3, 4):
			return "unit_4"
		if question_number in (5, 6):
			return "unit_5"
		if question_number in (7, 8):
			return "unit_6"
	elif exam_type == "Insem":
		if question_number in (1, 2):
			return "unit_1"
		if question_number in (3, 4):
			return "unit_2"

	raise ValueError(f"Unsupported question_id for exam type {exam_type}: {question_id}")


async def get_embedding(session, record):
	# Rate check and timestamp registration under lock
	async with rate_lock:
		loop = asyncio.get_running_loop()
		now = loop.time()

		# Drop timestamps older than 60 seconds
		while request_times and now - request_times[0] > 60:
			request_times.popleft()

		# If at rate limit, calculate wait time
		if len(request_times) >= RATE_LIMIT:
			wait_time = 60 - (now - request_times[0])
		else:
			wait_time = 0

		# Register this request's timestamp before releasing lock
		request_times.append(loop.time())

	# Sleep outside lock so other coroutines can proceed
	if wait_time > 0:
		await asyncio.sleep(wait_time)

	async with semaphore:
		payload = {
			"model": "models/gemini-embedding-2",
			"content": {
				"parts": [{"text": record["text"]}]
			}
		}

		try:
			async with session.post(
				EMBEDDING_API_URL,
				headers={
					"Content-Type": "application/json",
					"x-goog-api-key": GOOGLE_API_KEY
				},
				json=payload
			) as resp:
				if resp.status == 429:
					error_text = await resp.text()
					print(f"[ERROR] Google API rate limit hit for record '{record['question_id']}': {error_text}", file=sys.stderr)
					raise RuntimeError(f"GOOGLE_API_RATE_LIMIT: Rate limit hit for record '{record['question_id']}'")
				elif resp.status != 200:
					error_text = await resp.text()
					print(f"[ERROR] Google Embedding API error {resp.status} for record '{record['question_id']}': {error_text}", file=sys.stderr)
					raise RuntimeError(f"API error {resp.status} for record '{record['question_id']}': {error_text}")

				try:
					data = await resp.json()
				except Exception as e:
					raise RuntimeError(f"Failed to parse API response for record '{record['question_id']}': {e}")

				if "embedding" not in data or "values" not in data.get("embedding", {}):
					raise RuntimeError(f"Unexpected API response structure for record '{record['question_id']}': {data}")

				embedding = data["embedding"]["values"]
				return {
					"question_id": record["question_id"],
					"text": record["text"],
					"marks": record.get("marks"),
					"embedding": embedding,
				}
		except aiohttp.ClientConnectionError as e:
			print(f"[ERROR] Network connection error for record '{record['question_id']}': {e}", file=sys.stderr)
			raise RuntimeError(f"NETWORK_ERROR: Failed to connect to Google Embedding API for '{record['question_id']}': {e}")
		except aiohttp.ClientTimeout as e:
			print(f"[ERROR] Request timed out for record '{record['question_id']}': {e}", file=sys.stderr)
			raise RuntimeError(f"NETWORK_ERROR: Request timed out for '{record['question_id']}': {e}")
		except RuntimeError:
			raise
		except Exception as e:
			print(f"[ERROR] Unexpected error for record '{record['question_id']}': {e}", file=sys.stderr)
			raise RuntimeError(f"Unexpected error embedding record '{record['question_id']}': {e}")


async def main(subject_name: str, exam_type: str = DEFAULT_EXAM_TYPE, user_id: str = None):
	global GOOGLE_API_KEY
	if user_id:
		try:
			GOOGLE_API_KEY = fetch_google_api_key(user_id)
		except Exception as e:
			err_str = str(e)
			print(f"[FATAL] Failed to fetch Google API key: {err_str}", file=sys.stderr)
			if "network error" in err_str.lower():
				sys.exit(3)  # EXIT_NETWORK
			sys.exit(1)  # EXIT_LOGICAL

	subject_folder = _safe_folder_name(subject_name)
	papers_root = Path("subjects") / subject_folder / "papers"

	try:
		input_files = discover_input_files(papers_root)
	except OSError as e:
		print(f"[FATAL] Failed to discover input files in '{papers_root}': {e}", file=sys.stderr)
		sys.exit(1)

	if not input_files:
		print(f"[FATAL] No subquestions.json files found under {papers_root}", file=sys.stderr)
		sys.exit(1)

	records = []
	for input_file in input_files:
		try:
			raw_records = load_records_from_json(input_file)
			for idx, record in enumerate(raw_records):
				records.append(normalize_record(record, input_file, idx))
		except (RuntimeError, ValueError) as e:
			print(f"[FATAL] Failed to load records from '{input_file}': {e}", file=sys.stderr)
			sys.exit(1)

	print(f"Loaded {len(records)} records from {len(input_files)} files. Starting embedding generation...")

	try:
		async with aiohttp.ClientSession() as session:
			tasks = [get_embedding(session, record) for record in records]
			results = await asyncio.gather(*tasks, return_exceptions=True)
	except aiohttp.ClientConnectionError as e:
		print(f"[FATAL] Network error creating HTTP session: {e}", file=sys.stderr)
		sys.exit(3)  # EXIT_NETWORK
	except Exception as e:
		print(f"[FATAL] Unexpected error during embedding generation: {e}", file=sys.stderr)
		sys.exit(1)

	# Check for any failures in individual results
	has_rate_limit = False
	has_network_error = False
	has_failures = False

	for result in results:
		if isinstance(result, Exception):
			has_failures = True
			err_str = str(result)
			print(f"[ERROR] Embedding task failed: {err_str}", file=sys.stderr)
			if "GOOGLE_API_RATE_LIMIT" in err_str:
				has_rate_limit = True
			elif "NETWORK_ERROR" in err_str:
				has_network_error = True

	if has_failures:
		if has_rate_limit:
			sys.exit(2)  # EXIT_RATE_LIMIT
		elif has_network_error:
			sys.exit(3)  # EXIT_NETWORK
		else:
			sys.exit(1)  # EXIT_LOGICAL

	# Filter out any exception results just in case (shouldn't reach here if has_failures check passed)
	valid_results = [r for r in results if not isinstance(r, Exception)]

	output_root = Path("subjects") / subject_folder / "temporary_embedddings_storage"
	try:
		output_root.mkdir(parents=True, exist_ok=True)
	except OSError as e:
		print(f"[FATAL] Failed to create output directory '{output_root}': {e}", file=sys.stderr)
		sys.exit(1)

	unit_buckets = {}
	for result in valid_results:
		try:
			unit_name = get_unit_name(exam_type, result["question_id"])
			unit_buckets.setdefault(unit_name, []).append(result)
		except ValueError as e:
			print(f"[WARNING] Could not determine unit for '{result['question_id']}': {e}", file=sys.stderr)

	for unit_name, unit_records in unit_buckets.items():
		output_path = output_root / f"{unit_name}.json"
		try:
			with output_path.open("w", encoding="utf-8") as f:
				json.dump(unit_records, f, ensure_ascii=False, indent=2)
		except OSError as e:
			print(f"[FATAL] Failed to write unit embeddings to '{output_path}': {e}", file=sys.stderr)
			sys.exit(1)

	print(
		f"Done. {len(valid_results)} embeddings saved across {len(unit_buckets)} files in {output_root}"
	)


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Generate embeddings for one subject")
	parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
	parser.add_argument("exam_type", nargs="?", default=DEFAULT_EXAM_TYPE, help="Exam type: Endsem or Insem")
	parser.add_argument("user_id", help="User ID of the uploader")
	args = parser.parse_args()

	asyncio.run(main(args.subject, exam_type=args.exam_type, user_id=args.user_id))