#!/usr/bin/env python3
"""
Takes snapshots of sub-questions from HTML using semantic markers.
Uses Playwright to capture screenshots between markers.

Rules:
1. Start from Q1)a) marker
2. Screenshot from marker A to marker B (including A, excluding B)
3. Last marker goes to page end
4. Save as Q{num}_{sub}.png in snapshots folder

Usage:
    pip install playwright
    playwright install chromium
    python take_snapshots.py
"""

import os
import sys
import asyncio
import argparse
from playwright.async_api import async_playwright
from pathlib import Path


async def take_snapshots(page, html_file: str, output_folder: str, label: str = ""):
    """
    Take snapshots of sub-questions from HTML file using semantic markers.
    
    Args:
        html_file: Path to the HTML file with semantic markers
        output_folder: Folder to save snapshots
    """
    # Create output folder if it doesn't exist
    try:
        Path(output_folder).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[ERROR] Failed to create output folder '{output_folder}': {e}", file=sys.stderr)
        raise RuntimeError(f"Cannot create snapshot output directory: {e}")

    # Load the HTML file with networkidle to ensure all resources are loaded
    html_path = Path(html_file).resolve()

    try:
        await page.goto(html_path.as_uri(), wait_until='networkidle')
    except Exception as e:
        err_str = str(e).lower()
        if any(keyword in err_str for keyword in ("timeout", "net::", "connection", "refused")):
            raise RuntimeError(f"NETWORK_ERROR: Failed to load HTML file '{html_file}': {e}")
        raise RuntimeError(f"Failed to load HTML page for '{html_file}': {e}")

    prefix = f"[{label}] " if label else ""
    print(f"{prefix}All resources loaded. Starting snapshot extraction...")

    # Get all markers sorted by their position on the page
    try:
        markers = await page.evaluate('''() => {
                const markerElements = Array.from(document.querySelectorAll('.subq-marker'));
                return markerElements.map(el => {
                    return {
                        question: el.getAttribute('data-q'),
                        subQuestion: el.getAttribute('data-sub')
                    };
                }).sort((a, b) => {
                    // Sort by question number, then by sub-question letter
                    if (a.question !== b.question) {
                        return parseInt(a.question) - parseInt(b.question);
                    }
                    return a.subQuestion.localeCompare(b.subQuestion);
                });
            }''')
    except Exception as e:
        raise RuntimeError(f"Failed to evaluate page markers for '{html_file}': {e}")

    if not markers:
        print(f"{prefix}Error: No markers found in the HTML file.")
        return 0

    print(f"{prefix}Found {len(markers)} markers")

    captured = 0

    # Take snapshots between consecutive markers using element-based screenshots
    for i in range(len(markers)):
        marker = markers[i]
        q_num = marker['question']
        sub_q = marker['subQuestion']

        # Determine the next marker (or use end of document)
        next_selector = None
        if i < len(markers) - 1:
            next_marker = markers[i + 1]
            next_q = next_marker['question']
            next_sub = next_marker['subQuestion']
            next_selector = f'[data-q="{next_q}"][data-sub="{next_sub}"]'

        # Create a temporary wrapper div around content between markers and screenshot it
        try:
            screenshot_result = await page.evaluate(f'''(nextSelector) => {{
                    const currentMarker = document.querySelector('[data-q="{q_num}"][data-sub="{sub_q}"]');
                    if (!currentMarker) return {{ success: false, error: 'Marker not found' }};
                    
                    // Find the next marker element (or use null for last marker)
                    const nextMarker = nextSelector ? document.querySelector(nextSelector) : null;
                    
                    // Create a wrapper div
                    const wrapper = document.createElement('div');
                    wrapper.id = 'temp-screenshot-wrapper';
                    wrapper.style.display = 'block';
                    
                    // Find the block-level parent element containing the current marker
                    // This is typically a <p>, <div>, or similar block element
                    let startElement = currentMarker.parentElement;
                    while (startElement && startElement !== document.body) {{
                        const display = window.getComputedStyle(startElement).display;
                        const tagName = startElement.tagName;
                        // Check if this is a block-level element
                        if (display === 'block' || display === 'list-item' || 
                            tagName === 'P' || tagName === 'DIV' || tagName === 'LI' ||
                            tagName === 'H1' || tagName === 'H2' || tagName === 'H3' ||
                            tagName === 'H4' || tagName === 'H5' || tagName === 'H6') {{
                            break;
                        }}
                        startElement = startElement.parentElement;
                    }}
                    
                    if (!startElement || startElement === document.body) {{
                        startElement = currentMarker.parentElement;
                    }}
                    
                    // Find the block-level parent of the next marker (if it exists)
                    let nextStartElement = null;
                    if (nextMarker) {{
                        nextStartElement = nextMarker.parentElement;
                        while (nextStartElement && nextStartElement !== document.body) {{
                            const display = window.getComputedStyle(nextStartElement).display;
                            const tagName = nextStartElement.tagName;
                            if (display === 'block' || display === 'list-item' || 
                                tagName === 'P' || tagName === 'DIV' || tagName === 'LI' ||
                                tagName === 'H1' || tagName === 'H2' || tagName === 'H3' ||
                                tagName === 'H4' || tagName === 'H5' || tagName === 'H6') {{
                                break;
                            }}
                            nextStartElement = nextStartElement.parentElement;
                        }}
                        if (!nextStartElement || nextStartElement === document.body) {{
                            nextStartElement = nextMarker.parentElement;
                        }}
                    }}
                    
                    // Collect elements starting from startElement until we hit nextStartElement
                    const nodesToWrap = [];
                    let currentNode = startElement;
                    
                    // Walk through siblings starting from startElement
                    while (currentNode && currentNode !== document.body) {{
                        // Stop if we've reached the element containing the next marker
                        if (nextStartElement && currentNode === nextStartElement) {{
                            break;
                        }}
                        
                        nodesToWrap.push(currentNode);
                        
                        // Move to next sibling
                        currentNode = currentNode.nextSibling;
                        
                        // If no next sibling, we're done (reached end of container)
                        if (!currentNode) {{
                            break;
                        }}
                    }}
                    
                    // If we collected nothing, at least include the start element
                    if (nodesToWrap.length === 0) {{
                        nodesToWrap.push(startElement);
                    }}
                    
                    // Insert wrapper before the first node
                    if (nodesToWrap.length > 0) {{
                        const firstNode = nodesToWrap[0];
                        firstNode.parentNode.insertBefore(wrapper, firstNode);
                        
                        // Move all nodes into wrapper
                        nodesToWrap.forEach(node => {{
                            wrapper.appendChild(node);
                        }});
                    }}
                    
                    return {{ success: true }};
                }}''', next_selector)
        except Exception as e:
            print(f"{prefix}⚠ Error evaluating page for Q{q_num}_{sub_q}: {e}", file=sys.stderr)
            continue

        if not screenshot_result.get('success'):
            print(f"{prefix}⚠ Skipping Q{q_num}_{sub_q}: {screenshot_result.get('error', 'Unknown error')}")
            continue

        # Filename for the snapshot
        filename = f"Q{q_num}_{sub_q}.png"
        filepath = os.path.join(output_folder, filename)

        try:
            # Get the wrapper element and take element-based screenshot
            wrapper = await page.query_selector('#temp-screenshot-wrapper')
            if wrapper:
                await wrapper.screenshot(path=filepath)
                captured += 1
                print(f"{prefix}✓ Captured: {filename}")
            else:
                print(f"{prefix}✗ Failed to capture {filename}: Wrapper not found")

        except Exception as e:
            print(f"{prefix}✗ Failed to capture {filename}: {str(e)}", file=sys.stderr)
        finally:
            # Clean up: unwrap the nodes and remove wrapper
            try:
                await page.evaluate('''() => {
                        const wrapper = document.getElementById('temp-screenshot-wrapper');
                        if (wrapper && wrapper.parentNode) {
                            while (wrapper.firstChild) {
                                wrapper.parentNode.insertBefore(wrapper.firstChild, wrapper);
                            }
                            wrapper.parentNode.removeChild(wrapper);
                        }
                    }''')
            except Exception as cleanup_err:
                print(f"{prefix}⚠ Cleanup error for Q{q_num}_{sub_q}: {cleanup_err}", file=sys.stderr)

    print(f"{prefix}✓ Saved {captured} snapshots to '{output_folder}'")
    return captured


def discover_papers(papers_root: str = "papers") -> list[tuple[Path, Path, str]]:
    """Return [(html_file, output_folder, paper_name)] for each paper with html/paper.html."""
    root = Path(papers_root)
    jobs: list[tuple[Path, Path, str]] = []

    if not root.exists() or not root.is_dir():
        return jobs

    try:
        paper_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as e:
        print(f"[ERROR] Failed to list paper directories in '{root}': {e}", file=sys.stderr)
        return jobs

    for paper_dir in paper_dirs:
        html_file = paper_dir / "html" / "paper.html"
        output_folder = paper_dir / "html" / "question_snapshots"
        if html_file.exists() and html_file.is_file():
            jobs.append((html_file, output_folder, paper_dir.name))

    return jobs


async def process_paper(browser, html_file: Path, output_folder: Path, paper_name: str, semaphore: asyncio.Semaphore) -> int:
    """Process one paper in isolation with its own browser context/page."""
    async with semaphore:
        try:
            context = await browser.new_context(viewport={'width': 1200, 'height': 3000})
        except Exception as e:
            raise RuntimeError(f"Failed to create browser context for '{paper_name}': {e}")

        page = await context.new_page()
        try:
            return await take_snapshots(
                page=page,
                html_file=str(html_file),
                output_folder=str(output_folder),
                label=paper_name,
            )
        finally:
            try:
                await context.close()
            except Exception as e:
                print(f"[WARNING] Failed to close browser context for '{paper_name}': {e}", file=sys.stderr)


async def process_all_papers(papers_root: str = "papers", max_concurrency: int = 3):
    """Process snapshots for every papers/<paper>/html/paper.html concurrently."""
    jobs = discover_papers(papers_root)
    if not jobs:
        print(f"No paper.html files found under {papers_root}")
        return

    print(f"Discovered {len(jobs)} paper HTML files")

    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:
                raise RuntimeError(f"Failed to launch Chromium browser: {e}")

            try:
                tasks = [
                    asyncio.create_task(process_paper(browser, html_file, output_folder, paper_name, semaphore))
                    for html_file, output_folder, paper_name in jobs
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                try:
                    await browser.close()
                except Exception as e:
                    print(f"[WARNING] Failed to close browser: {e}", file=sys.stderr)
    except Exception as e:
        err_str = str(e).lower()
        if any(keyword in err_str for keyword in ("executable", "chromium", "playwright", "browser", "launch")):
            raise RuntimeError(f"Browser launch failure: {e}")
        raise

    total = 0
    any_failed = False
    for (html_file, _, paper_name), result in zip(jobs, results):
        if isinstance(result, Exception):
            print(f"[{paper_name}] ✗ Failed: {result}", file=sys.stderr)
            any_failed = True
        else:
            total += result
            print(f"[{paper_name}] Done from {html_file}")

    print(f"\n✓ Completed all papers. Total snapshots captured: {total}")

    if any_failed:
        raise RuntimeError("One or more papers failed during snapshot capture.")


async def main(subject_name: str):
    """Main entry point"""
    papers_root = Path("subjects") / subject_name / "papers"
    if not papers_root.exists() or not papers_root.is_dir():
        print(f"[FATAL] papers folder not found for subject '{subject_name}': {papers_root}", file=sys.stderr)
        sys.exit(1)

    try:
        await process_all_papers(str(papers_root), max_concurrency=3)
    except RuntimeError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[FATAL] Unexpected error during snapshot capture: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capture question snapshots for one subject")
    parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
    args = parser.parse_args()

    asyncio.run(main(args.subject))