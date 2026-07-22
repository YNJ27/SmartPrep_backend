import base64
import argparse
import json
import os
import re
import sys
import time
import threading
from pathlib import Path
from mistralai.client import Mistral
from security.encryption import decrypt_api_key
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
except Exception as e:
    print(f"[ERROR] Failed to initialize Supabase client: {e}", file=sys.stderr)
    supabase = None

def get_mistral_client(user_id: str):
    if not supabase:
        raise Exception("Supabase client not initialized")
    try:
        res = supabase.table("user_api_keys").select("encrypted_api_key").eq("user_id", user_id).eq("provider", "Mistral").execute()
    except Exception as e:
        raise Exception(f"Network error while fetching Mistral API key: {e}")
    if not res.data:
        raise Exception(f"No Mistral API key found for user {user_id}")
    encrypted_key = res.data[0]["encrypted_api_key"]
    try:
        api_key = decrypt_api_key(encrypted_key)
    except Exception as e:
        raise Exception(f"Failed to decrypt Mistral API key: {e}")
    return Mistral(api_key=api_key)

client = None

# Thread-safe failure tracking
_failed_pdfs: list[str] = []
_failed_pdfs_lock = threading.Lock()


def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "subject"


def infer_pdf_folder(pdf_path, paper_index):
    """Return per-PDF folder name (just the PDF filename without extension)."""
    stem = Path(pdf_path).stem
    return stem if stem else f"paper_{paper_index + 1:03d}"


def create_paper_structure(papers_dir, pdf_folder_name, source_pdf_path):
    """Create folder scaffold matching the required structure for one paper (no year grouping)."""
    paper_root = papers_dir / pdf_folder_name
    ocr_dir = paper_root / "ocr"
    html_dir = paper_root / "html"
    question_snapshots_dir = html_dir / "question_snapshots"
    questions_dir = paper_root / "questions"

    try:
        ocr_dir.mkdir(parents=True, exist_ok=True)
        question_snapshots_dir.mkdir(parents=True, exist_ok=True)
        questions_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"Failed to create directory structure for {pdf_folder_name}: {e}")

    return {
        "paper_root": paper_root,
        "ocr_dir": ocr_dir,
        "html_dir": html_dir,
        "question_snapshots_dir": question_snapshots_dir,
        "questions_dir": questions_dir,
    }

def process_markdown(text):
    # Match b), c), d) only when they appear at the start of a line or after whitespace
    # and are followed by a space or text (not inside parentheses)
    text = re.sub(r'(?<!\n\n)^\s*([bcd]\)\s)', r'\n\1', text, flags=re.MULTILINE)
    
    # Match question numbers like Q1), Q2), etc.
    text = re.sub(r'(?<!\n\n)^\s*(Q\d+\)\s)', r'\n\1', text, flags=re.MULTILINE)
    
    return text

def clean_markdown(text):
    # Remove "OR" wherever it appears
    text = re.sub(r'OR', '', text)
    # Remove "P.T.O." and "P.T.O" wherever they appear
    text = re.sub(r'P\.T\.O\.?', '', text)
    # Remove codes [X]-Y wherever they appear
    text = re.sub(r'\[[A-Za-z0-9]{4}\]-[A-Za-z0-9]+', '', text)
    # Remove codes [X] - Y wherever they appear
    text = re.sub(r'\[[A-Za-z0-9]{4}\] - [A-Za-z0-9]+', '', text)
    # Remove lines that are only page numbers (digits)
    text = re.sub(r'^\d+$\n?', '', text, flags=re.MULTILINE)
    # Remove bold formatting (**text** and __text__)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    return text

def encode_pdf(pdf_path):
    try:
        with open(pdf_path, "rb") as pdf_file:
            return base64.b64encode(pdf_file.read()).decode('utf-8')
    except OSError as e:
        raise RuntimeError(f"Failed to read PDF file '{pdf_path}': {e}")

def process_pdf(pdf_path, paper_index, output_base_dir, delay_seconds=0):
    """Process a single PDF with rate limiting"""
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    
    pdf_name = Path(pdf_path).stem
    pdf_folder_name = infer_pdf_folder(pdf_path, paper_index)
    print(f"Processing {pdf_folder_name}: {pdf_name}")
    
    try:
        # Encode PDF
        base64_pdf = encode_pdf(pdf_path)
        
        # Process OCR
        print(f"  Sending OCR request for {pdf_folder_name}...")
        try:
            ocr_response = client.ocr.process(
                model="mistral-ocr-latest",
                document={
                    "type": "document_url",
                    "document_url": f"data:application/pdf;base64,{base64_pdf}" 
                },
                table_format="html",
                include_image_base64=True
            )
        except Exception as e:
            err_str = str(e).lower()
            # Detect network-related errors from Mistral client
            if any(keyword in err_str for keyword in ("connection", "timeout", "network", "unreachable", "refused", "resolve")):
                raise RuntimeError(f"NETWORK_ERROR: Failed to reach Mistral OCR API for '{pdf_folder_name}': {e}")
            # Detect rate limit errors
            if any(keyword in err_str for keyword in ("rate limit", "429", "too many requests", "quota")):
                raise RuntimeError(f"RATE_LIMIT_ERROR: Mistral API rate limit hit for '{pdf_folder_name}': {e}")
            raise RuntimeError(f"Mistral OCR API error for '{pdf_folder_name}': {e}")
        
        # Create the required output structure for this paper.
        structure = create_paper_structure(
            output_base_dir,
            pdf_folder_name,
            pdf_path
        )
        ocr_dir = structure["ocr_dir"]
        
        # Save JSON
        json_path = ocr_dir / "paper.json"
        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(ocr_response.model_dump(), f, indent=2)
        except OSError as e:
            raise RuntimeError(f"Failed to write JSON output for '{pdf_folder_name}': {e}")
        print(f"  Saved JSON: {json_path}")
        
        # Save markdown with correct image references
        md_path = ocr_dir / "paper.md"
        try:
            with open(md_path, 'w', encoding='utf-8') as f:
                for page in ocr_response.pages:
                    processed = process_markdown(page.markdown)
                    cleaned = clean_markdown(processed)
                    f.write(cleaned + '\n\n')
        except OSError as e:
            raise RuntimeError(f"Failed to write markdown output for '{pdf_folder_name}': {e}")
        print(f"  Saved markdown: {md_path}")
        
        print(f"  Completed {pdf_folder_name}: {pdf_name}\n")
        
    except RuntimeError as e:
        err_str = str(e)
        print(f"  ERROR processing {pdf_folder_name} ({pdf_name}): {err_str}\n", file=sys.stderr)
        with _failed_pdfs_lock:
            _failed_pdfs.append(f"{pdf_folder_name}: {err_str}")
    except Exception as e:
        print(f"  ERROR processing {pdf_folder_name} ({pdf_name}): {e}\n", file=sys.stderr)
        with _failed_pdfs_lock:
            _failed_pdfs.append(f"{pdf_folder_name}: {e}")

def main(subject_name: str, user_id: str):
    global client
    try:
        client = get_mistral_client(user_id)
    except Exception as e:
        err_str = str(e)
        print(f"[FATAL] Failed to initialize Mistral client: {err_str}", file=sys.stderr)
        if "network error" in err_str.lower() or "network_error" in err_str.lower():
            sys.exit(3)  # EXIT_NETWORK
        sys.exit(1)  # EXIT_LOGICAL
    
    subject_folder = _safe_folder_name(subject_name)
    subject_root = Path("subjects") / subject_folder

    # Define paths inside the selected subject folder
    pdfs_dir = subject_root / "raw_pdfs"
    output_dir = subject_root / "papers"
    
    # Ensure output directory exists
    try:
        subject_root.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[FATAL] Failed to create output directories: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Get all PDF files
    try:
        pdf_files = sorted(list(pdfs_dir.glob("*.pdf")))
    except OSError as e:
        print(f"[FATAL] Failed to list PDF files in '{pdfs_dir}': {e}", file=sys.stderr)
        sys.exit(1)
    
    if not pdf_files:
        print(f"[FATAL] No PDF files found in {pdfs_dir}", file=sys.stderr)
        sys.exit(1)
    
    print(f"Found {len(pdf_files)} PDF files to process")
    print(f"Rate limit: 1 RPS (starting requests 1 second apart)\n")
    
    # Process PDFs with rate limiting using threads
    threads = []
    for index, pdf_path in enumerate(pdf_files):
        # Start each request 1 second apart (index * 1 second delay)
        thread = threading.Thread(
            target=process_pdf,
            args=(pdf_path, index, output_dir, index)
        )
        threads.append(thread)
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    if _failed_pdfs:
        print(f"\n[FATAL] {len(_failed_pdfs)} PDF(s) failed to process:", file=sys.stderr)
        has_rate_limit = False
        has_network = False
        for failure in _failed_pdfs:
            print(f"  - {failure}", file=sys.stderr)
            if "RATE_LIMIT_ERROR" in failure:
                has_rate_limit = True
            elif "NETWORK_ERROR" in failure:
                has_network = True
        if has_rate_limit:
            sys.exit(2)  # EXIT_RATE_LIMIT
        elif has_network:
            sys.exit(3)  # EXIT_NETWORK
        else:
            sys.exit(1)  # EXIT_LOGICAL

    print(f"\nAll PDFs processed! Output saved to: {output_dir}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Mistral OCR for one subject folder")
    parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
    parser.add_argument("user_id", help="User ID of the uploader")
    args = parser.parse_args()

    main(args.subject, args.user_id)