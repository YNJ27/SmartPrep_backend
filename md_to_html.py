import re
import html
import argparse
import sys
import markdown
from pathlib import Path

# ── Compiled patterns (once at module level) ──────────────────────────────────
DISPLAY_MATH_RE = re.compile(r"\$\$(.*?)\$\$", re.DOTALL)
INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?![`$])([^$\n]+?)(?<!\$)\$(?!\$)")
MAIN_Q_PATTERN = re.compile(r"^(\s*)(Q\s*(\d+)\))", re.IGNORECASE)
SUB_Q_PATTERN = re.compile(r"^(\s*)([b-d]\))", re.IGNORECASE)
IMG_TAG_PATTERN = re.compile(r"<img([^>]*?)src=[\"']([^\"']+)[\"']", re.IGNORECASE)

# ── Single shared Markdown engine ─────────────────────────────────────────────
MD_ENGINE = markdown.Markdown(extensions=["tables"])


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Markdown Preview</title>
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<style>
body {{
    font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    margin: 20px;
    max-width: 900px;
    line-height: 1.6;
}}
img {{
    max-width: 100%;
    height: auto;
    display: block;
    margin: 12px 0;
    border: 1px solid #ddd;
    border-radius: 4px;
    padding: 5px;
}}
table {{
    border-collapse: collapse;
    margin-top: 12px;
    width: auto;
}}
th, td {{
    border: 1px solid #444;
    padding: 6px 10px;
    text-align: left;
    vertical-align: top;
}}
th {{
    background: #f2f2f2;
}}
.katex-display {{
    margin: 12px 0;
}}
.subq-marker {{
    display: inline-block;
    font-weight: bold;
    margin-right: 4px;
}}
</style>
</head>
<body>
{content}

<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"
    onload="renderMathInElement(document.body, {{
        delimiters: [
            {{left: '$$', right: '$$', display: true}},
            {{left: '$',  right: '$',  display: false}}
        ],
        throwOnError: false
    }});"></script>
</body>
</html>
"""


def protect_math(text: str) -> tuple[str, dict]:
    """
    Extract all math expressions and replace with unique placeholders.
    This prevents markdown from corrupting $...$ and $$...$$ content.
    Returns (modified_text, placeholder_map).
    """
    placeholders = {}
    counter = [0]

    def make_placeholder(expr: str) -> str:
        # Placeholder must not contain chars markdown will process
        key = f"MATHX{counter[0]}XMATH"
        counter[0] += 1
        placeholders[key] = expr
        return key

    # Display math first (before inline, to avoid partial matches)
    def repl_display(m):
        inner = html.unescape(m.group(1).strip("\n"))
        return make_placeholder(f"$${inner}$$")

    text = DISPLAY_MATH_RE.sub(repl_display, text)

    # Inline math
    def repl_inline(m):
        inner = html.unescape(m.group(1))
        return make_placeholder(f"${inner}$")

    text = INLINE_MATH_RE.sub(repl_inline, text)

    return text, placeholders


def restore_math(html_body: str, placeholders: dict) -> str:
    """Replace placeholders back with original math expressions."""
    for key, expr in placeholders.items():
        html_body = html_body.replace(key, expr)
    return html_body


def inject_semantic_markers(text: str) -> str:
    current_question = None
    lines = text.split("\n")
    processed_lines = []

    for line in lines:
        m_main = MAIN_Q_PATTERN.match(line)
        if m_main:
            indent, full_marker, q_num = m_main.group(1), m_main.group(2), m_main.group(3)
            current_question = q_num
            rest = line[m_main.end():]
            # Insert blank line before each new question so Markdown creates a fresh <p>
            if processed_lines and processed_lines[-1].strip() != "":
                processed_lines.append("")
            line = (
                f"{indent}"
                f'<span class="subq-marker" data-q="{q_num}" data-sub="a">{full_marker}</span>'
                f"{rest}"
            )

        m_sub = SUB_Q_PATTERN.match(line)
        if m_sub and current_question is not None:
            indent, sub_marker = m_sub.group(1), m_sub.group(2)
            sub_letter = sub_marker[0]
            rest = line[m_sub.end():]
            # Insert blank line before each sub-question so Markdown creates a fresh <p>
            if processed_lines and processed_lines[-1].strip() != "":
                processed_lines.append("")
            line = (
                f"{indent}"
                f'<span class="subq-marker" data-q="{current_question}" data-sub="{sub_letter}">{sub_marker}</span>'
                f"{rest}"
            )

        processed_lines.append(line)

    return "\n".join(processed_lines)


def normalize_line_breaks(text: str) -> str:
    lines = text.split("\n")
    result = []
    for i, line in enumerate(lines):
        if i < len(lines) - 1:
            next_line = lines[i + 1]
            if (
                line.strip() == ""
                or next_line.strip() == ""
                or line.strip().startswith("|")
                or next_line.strip().startswith("|")
                or line.strip().startswith("```")
                or line.endswith("  ")
                # Don't force-join across subquestion boundaries
                or line.strip().startswith('<span class="subq-marker"')
                or next_line.strip().startswith('<span class="subq-marker"')
            ):
                result.append(line)
            else:
                result.append(line + "  ")
        else:
            result.append(line)
    return "\n".join(result)


def load_cloud_url_map(json_file: str) -> dict:
    import json

    try:
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    url_map = {}
    for page in data.get("pages", []):
        for image in page.get("images", []):
            image_id = image.get("id")
            cloud_url = image.get("cloud_url")
            if image_id and cloud_url:
                url_map[image_id] = cloud_url

    return url_map


def apply_cloud_urls(html_body: str, url_map: dict) -> str:
    if "<img" not in html_body:
        return html_body

    def repl(match):
        before_src, src = match.group(1), match.group(2)
        if re.match(r"^(?:https?:)?//|^data:", src, flags=re.IGNORECASE):
            return match.group(0)
        src_name = Path(src).name
        cloud_url = url_map.get(src) or url_map.get(src_name)
        if not cloud_url:
            return match.group(0)
        return f"<img{before_src}src=\"{cloud_url}\""

    return IMG_TAG_PATTERN.sub(repl, html_body)


def convert_md_to_html(
    md_file: str = r"2_OCR_outputs_of_one_subject\ocr_outputs\paper_1\p1.md",
    html_file: str = "ocr_output.html",
    json_file: str = r"2_OCR_outputs_of_one_subject\ocr_outputs\paper_1\p1.json",
):
    import json
    import sys

    try:
        with open(md_file, "r", encoding="utf-8") as f:
            md_text = f.read()
    except OSError as e:
        print(f"[ERROR] Failed to read markdown file '{md_file}': {e}", file=sys.stderr)
        raise

    # ── Step 1: Semantic markers ───────────────────────────────────────────────
    md_text = inject_semantic_markers(md_text)

    # ── Step 2: Protect math BEFORE markdown touches it ───────────────────────
    md_text, math_placeholders = protect_math(md_text)

    # ── Step 3: Normalize line breaks ─────────────────────────────────────────
    md_text = normalize_line_breaks(md_text)

    # ── Step 4: Render markdown ────────────────────────────────────────────────
    MD_ENGINE.reset()
    html_body = MD_ENGINE.convert(md_text)

    # ── Step 5: Restore math expressions ──────────────────────────────────────
    html_body = restore_math(html_body, math_placeholders)

    # ── Step 6: Replace image refs with cloud URLs ─────────────────────────────
    url_map = load_cloud_url_map(json_file)
    html_body = apply_cloud_urls(html_body, url_map)

    # ── Step 7: Embed OCR tables ──────────────────────────────────────────────
    tables_dict = {}
    json_file = json_file or "ocr_response.json"

    if 'href="tbl' in html_body:  # ← FIXED: was 'href="tbl_', missed hyphen IDs
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for page in data.get("pages", []):
                for tbl in page.get("tables", []):
                    tables_dict[tbl["id"]] = tbl["content"]
            print(f"Loaded {len(tables_dict)} tables")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Warning: {e}", file=sys.stderr)

    if tables_dict:
        table_pattern = re.compile(
            r'<a href="(' + "|".join(map(re.escape, tables_dict)) + r')">\1</a>'
        )
        html_body = table_pattern.sub(lambda m: tables_dict[m.group(1)], html_body)
        print(f"Embedded {len(tables_dict)} tables into HTML")

    # ── Step 8: Write output ──────────────────────────────────────────────────
    try:
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(HTML_TEMPLATE.format(content=html_body))
    except OSError as e:
        print(f"[ERROR] Failed to write HTML output to '{html_file}': {e}", file=sys.stderr)
        raise

    print(f"[OK] {md_file} -> {html_file}")


def convert_paper_folder(paper_dir: Path) -> None:
    """Convert one paper folder using ocr/paper.md + ocr/paper.json -> html/paper.html."""
    import json
    import sys

    paper_dir = Path(paper_dir)
    ocr_dir = paper_dir / "ocr"
    html_dir = paper_dir / "html"

    md_file = ocr_dir / "paper.md"
    json_file = ocr_dir / "paper.json"
    html_file = html_dir / "paper.html"

    if not md_file.exists():
        print(f"Skipping {paper_dir.name}: missing {md_file}")
        return

    try:
        html_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[ERROR] Failed to create HTML directory '{html_dir}': {e}", file=sys.stderr)
        raise RuntimeError(f"Cannot create HTML output directory for '{paper_dir.name}': {e}")

    try:
        with open(md_file, "r", encoding="utf-8") as f:
            md_text = f.read()
    except OSError as e:
        print(f"[ERROR] Failed to read markdown file '{md_file}': {e}", file=sys.stderr)
        raise RuntimeError(f"Cannot read markdown for '{paper_dir.name}': {e}")

    md_text = inject_semantic_markers(md_text)
    md_text, math_placeholders = protect_math(md_text)
    md_text = normalize_line_breaks(md_text)

    MD_ENGINE.reset()
    html_body = MD_ENGINE.convert(md_text)
    html_body = restore_math(html_body, math_placeholders)

    url_map = load_cloud_url_map(str(json_file))
    html_body = apply_cloud_urls(html_body, url_map)

    tables_dict = {}
    if 'href="tbl' in html_body:
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for page in data.get("pages", []):
                for tbl in page.get("tables", []):
                    tables_dict[tbl["id"]] = tbl["content"]
            print(f"{paper_dir.name}: loaded {len(tables_dict)} tables")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"{paper_dir.name}: warning: {e}", file=sys.stderr)

    if tables_dict:
        table_pattern = re.compile(
            r'<a href="(' + "|".join(map(re.escape, tables_dict)) + r')">\1</a>'
        )
        html_body = table_pattern.sub(lambda m: tables_dict[m.group(1)], html_body)
        print(f"{paper_dir.name}: embedded {len(tables_dict)} tables")

    try:
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(HTML_TEMPLATE.format(content=html_body))
    except OSError as e:
        print(f"[ERROR] Failed to write HTML file '{html_file}': {e}", file=sys.stderr)
        raise RuntimeError(f"Cannot write HTML output for '{paper_dir.name}': {e}")

    print(f"[OK] {md_file} -> {html_file}")


def convert_all_papers(papers_root: str = "papers") -> None:
    """Convert every paper folder in papers_root."""
    root = Path(papers_root)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"papers root not found: {root}")

    try:
        paper_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as e:
        raise RuntimeError(f"Failed to list paper directories in '{root}': {e}")

    if not paper_dirs:
        print(f"No paper folders found in {root}")
        return

    any_failed = False
    for paper_dir in paper_dirs:
        try:
            convert_paper_folder(paper_dir)
        except RuntimeError as e:
            print(f"[ERROR] Failed to convert paper '{paper_dir.name}': {e}", file=sys.stderr)
            any_failed = True
        except Exception as e:
            print(f"[ERROR] Unexpected error converting paper '{paper_dir.name}': {e}", file=sys.stderr)
            any_failed = True

    if any_failed:
        raise RuntimeError("One or more paper conversions failed. See errors above.")


def main(subject_name: str) -> None:
    subject_root = Path("subjects") / subject_name
    papers_root = subject_root / "papers"

    if not papers_root.exists() or not papers_root.is_dir():
        print(f"[FATAL] papers folder not found for subject '{subject_name}': {papers_root}", file=sys.stderr)
        sys.exit(1)

    try:
        convert_all_papers(str(papers_root))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[FATAL] Unexpected error during markdown-to-HTML conversion: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert paper markdown to HTML for one subject")
    parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
    args = parser.parse_args()

    main(args.subject)
