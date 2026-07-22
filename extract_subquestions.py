import os
import re
import json
import sys
import argparse
from pathlib import Path


def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "subject"

def extract_subquestions(file_path, output_dir=None, paper_name=None):

    # Read the file
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError as e:
        print(f"[ERROR] Failed to read file '{file_path}': {e}", file=sys.stderr)
        return 0
    
    # Find the starting position (Q1))
    start_pattern = r'^\s*Q\s*1\)'
    start_match = re.search(start_pattern, content, flags=re.MULTILINE | re.IGNORECASE)
    
    if not start_match:
        print("Could not find starting point: Q1)")
        return 0
    
    # Start from Q1)
    content = content[start_match.start():]
    
    # Pattern for question numbers (Q1), Q2), etc.) - matches at start of line
    question_pattern = r'^\s*Q\s*(\d+)\)'
    # Pattern for subquestion letters (b), c), d)) - matches at start of line
    subquestion_pattern = r'^\s*([b-d])\)'
    
    # Find all anchor positions with their types
    anchors = []
    question_anchors = []
    
    # Find all question number anchors
    for match in re.finditer(question_pattern, content, flags=re.MULTILINE | re.IGNORECASE):
        q_num = match.group(1)
        q_pos = match.start()
        question_anchors.append({
            'pos': q_pos,
            'value': q_num
        })
        # Add Q anchors to anchors list (used for "a)" subquestions)
        anchors.append({
            'pos': q_pos,
            'type': 'question',
            'value': q_num,
            'subquestion': 'a'  # Q anchors are for "a)" subquestions
        })
    
    # Find all subquestion letter anchors (b, c, d)
    for match in re.finditer(subquestion_pattern, content, flags=re.MULTILINE | re.IGNORECASE):
        # Find which question this belongs to
        prev_q = None
        for q_anchor in question_anchors:
            if q_anchor['pos'] < match.start():
                prev_q = q_anchor
            elif q_anchor['pos'] > match.start():
                break
        if prev_q:
            anchors.append({
                'pos': match.start(),
                'type': 'subquestion',
                'value': match.group(1).lower(),  # b, c, d
                'question': prev_q['value']
            })
    
    # Sort anchors by position
    anchors.sort(key=lambda x: x['pos'])
    
    # Pattern to remove tables and images
    table_pattern = r'\[tbl-\d+\.html\]\(tbl-\d+\.html\)'
    # Match images referenced directly by filename
    image_pattern = r'!\[img-\d+\.(?:jpeg|jpg|png|gif)\]\(img-\d+\.(?:jpeg|jpg|png|gif)\)'
    
    # Pattern to extract marks
    marks_pattern = r'\[(\d+)\]'

    # Extract subquestions
    subquestions = []
    
    # Process each anchor
    for i in range(len(anchors)):
        anchor = anchors[i]
        
        # Determine the end position - find next anchor (could be subquestion or question)
        end_pos = len(content)  # Default to EOF
        for j in range(i + 1, len(anchors)):
            end_pos = anchors[j]['pos']
            break
        
        # Extract text between anchors
        text = content[anchor['pos']:end_pos]
        
        if anchor['type'] == 'question':
            # For Q anchors, extract "a)" subquestion
            # Remove the Q anchor itself (e.g., "Q1)")
            text = re.sub(r'^\s*Q\s*' + re.escape(anchor['value']) + r'\)\s*', '', text, flags=re.MULTILINE | re.IGNORECASE)
            
            # Remove "a)" if it's present (could be on same line or next line)
            text = re.sub(r'^\s*a\)\s*', '', text, flags=re.MULTILINE | re.IGNORECASE)
            text = re.sub(r'\s+a\)\s*', ' ', text, flags=re.IGNORECASE)
            
            subquestion_value = 'a'
            question_value = f"Q{anchor['value']}"
        else:
            # For b, c, d anchors, remove the anchor itself
            text = re.sub(r'^\s*' + re.escape(anchor['value']) + r'\)\s*', '', text, flags=re.MULTILINE | re.IGNORECASE)
            text = re.sub(r'\s+' + re.escape(anchor['value']) + r'\)\s*', ' ', text, flags=re.IGNORECASE)
            
            subquestion_value = anchor['value'].lower()
            question_value = f"Q{anchor['question']}"
        
        # Remove any Q anchors that might be in the text
        text = re.sub(r'\n\s*Q\s*\d+\)\s*', '', text, flags=re.IGNORECASE)
        
        # Remove tables and images with logging
        tables_found = re.findall(table_pattern, text)
        images_found = re.findall(image_pattern, text)
        if tables_found:
            print(f"[LOG] Removed {len(tables_found)} table(s) from {question_value}_{subquestion_value}")
        if images_found:
            print(f"[LOG] Removed {len(images_found)} image(s) from {question_value}_{subquestion_value}")
        text = re.sub(table_pattern, '', text)
        text = re.sub(image_pattern, '', text)

        # Extract and remove marks
        marks_match = re.search(marks_pattern, text)
        marks_value = int(marks_match.group(1)) if marks_match else None
        text = re.sub(marks_pattern, '', text)
        
        # Replace newlines with spaces
        text = re.sub(r'\n+', ' ', text)
        
        # Clean up multiple spaces
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Remove trailing newlines (already handled by strip, but ensure)
        text = text.rstrip('\n')
        
        if text:
            subquestions.append({
                'title': f"{question_value}_{subquestion_value}",
                'content': text,
                'marks': marks_value
            })
    
    if output_dir:
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as e:
            print(f"[ERROR] Failed to create output directory '{output_dir}': {e}", file=sys.stderr)
            return 0

        output_items = []
        for sq in subquestions:
            question_id = f"{paper_name}_{sq['title']}" if paper_name else sq['title']
            output_items.append({
                "question_id": question_id,
                "text": sq['content'],
                "marks": sq['marks']
            })

        json_path = os.path.join(output_dir, "subquestions.json")
        try:
            with open(json_path, 'w', encoding='utf-8') as json_f:
                json.dump(output_items, json_f, indent=2)
        except OSError as e:
            print(f"[ERROR] Failed to write subquestions JSON to '{json_path}': {e}", file=sys.stderr)
            return 0
    else:
        # Print extracted subquestions
        for sq in subquestions:
            print(f"{sq['title']}")
            print(sq['content'])
            print()  # Empty line between subquestions

    return len(subquestions)

def _process_subject(subject_name: str) -> None:
    subject_folder = _safe_folder_name(subject_name)
    papers_root = Path("subjects") / subject_folder / "papers"

    if not papers_root.exists() or not papers_root.is_dir():
        print(f"[FATAL] Papers folder not found for subject '{subject_name}': {papers_root}", file=sys.stderr)
        sys.exit(1)

    total_extracted = 0
    any_failed = False

    try:
        paper_dirs = sorted(p for p in papers_root.iterdir() if p.is_dir())
    except OSError as e:
        print(f"[FATAL] Failed to list papers in '{papers_root}': {e}", file=sys.stderr)
        sys.exit(1)

    for paper_dir in paper_dirs:
        paper_name = paper_dir.name
        md_path = paper_dir / "ocr" / "paper.md"
        questions_dir = paper_dir / "questions"
        if md_path.is_file():
            try:
                count = extract_subquestions(str(md_path), output_dir=str(questions_dir), paper_name=paper_name)
                count = count if count is not None else 0
                print(f"Extracted {count} subquestions from paper: {paper_name}")
                total_extracted += count
            except Exception as e:
                print(f"[ERROR] Unexpected error processing paper '{paper_name}': {e}", file=sys.stderr)
                any_failed = True
        else:
            print(f"[WARNING] Skipping '{paper_name}': paper.md not found at '{md_path}'")

    print(f"\nTotal subquestions extracted from all papers: {total_extracted}\n")

    if any_failed:
        print("[FATAL] One or more papers failed during subquestion extraction.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract subquestions for one subject")
    parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
    args = parser.parse_args()

    _process_subject(args.subject)
