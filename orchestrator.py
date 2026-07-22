import subprocess
import multiprocessing
import time
import sys
import argparse

# Exit codes used by this orchestrator and all child scripts:
#   0 = success
#   1 = generic / logical failure
#   2 = Google API rate limit hit
#   3 = network error
EXIT_SUCCESS = 0
EXIT_LOGICAL = 1
EXIT_RATE_LIMIT = 2
EXIT_NETWORK = 3


def run_script(script_name, *args):
    """Runs a python script with fully inherited stdout/stderr so output streams
    live to the backend terminal. Checks the exit code to determine error type.

    Returns a tuple: (success: bool, error_type: str | None)
    error_type can be None, 'rate_limit', 'network', or 'logical'
    """
    print(f"--- Starting {script_name} with args {args} ---", flush=True)

    # stdout and stderr are NOT piped — they stream directly to the terminal.
    # This fixes the charmap UnicodeEncodeError and the stdout-buffer deadlock.
    result = subprocess.run(
        [sys.executable, script_name] + list(args),
    )

    returncode = result.returncode
    if returncode == EXIT_SUCCESS:
        print(f"--- Finished {script_name} successfully ---", flush=True)
        return True, None
    elif returncode == EXIT_RATE_LIMIT:
        print(f"!!! {script_name} failed: Google API rate limit hit !!!", flush=True)
        return False, "rate_limit"
    elif returncode == EXIT_NETWORK:
        print(f"!!! {script_name} failed: network error !!!", flush=True)
        return False, "network"
    else:
        print(f"!!! Error in {script_name} (exit code {returncode}). See output above. !!!", flush=True)
        return False, "logical"


def left_branch(subject, result_queue):
    """Executes the left branch of the workflow sequentially."""
    print("--- Starting Left Branch ---", flush=True)
    for script, script_args in [
        ("images_upload_to_cloud.py", (subject,)),
        ("md_to_html.py", (subject,)),
        ("snapshots_using_playwright.py", (subject,)),
        ("uploading_snapshots.py", (subject,)),
    ]:
        success, error_type = run_script(script, *script_args)
        if not success:
            result_queue.put(("left", error_type))
            return
    print("--- Finished Left Branch ---", flush=True)
    result_queue.put(("left", None))


def right_branch(subject, exam_type, user_id, result_queue):
    """Executes the right branch of the workflow sequentially."""
    print("--- Starting Right Branch ---", flush=True)
    for script, script_args in [
        ("extract_subquestions.py", (subject,)),
        ("embeddings_all_together.py", (subject, exam_type, user_id)),
    ]:
        success, error_type = run_script(script, *script_args)
        if not success:
            result_queue.put(("right", error_type))
            return
    print("--- Finished Right Branch ---", flush=True)
    result_queue.put(("right", None))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Orchestrator for processing question papers")
    parser.add_argument("subject", help="Subject folder name, e.g. Microcontrollers")
    parser.add_argument("exam_type", help="Exam type: Endsem or Insem")
    parser.add_argument("user_id", help="User ID of the uploader")
    args = parser.parse_args()

    subject = args.subject
    exam_type = args.exam_type
    user_id = args.user_id

    start_time = time.time()

    print(">>> Starting Orchestrator <<<", flush=True)

    # 1. Run the initial OCR script
    success, error_type = run_script("Mistral_ocr_pdf_to_md.py", subject, user_id)
    if not success:
        print(f"!!! Initial OCR script failed. Error type: {error_type} !!!", flush=True)
        if error_type == "rate_limit":
            sys.exit(EXIT_RATE_LIMIT)
        elif error_type == "network":
            sys.exit(EXIT_NETWORK)
        else:
            sys.exit(EXIT_LOGICAL)

    # 2. Run the two branches in parallel
    result_queue = multiprocessing.Queue()

    process1 = multiprocessing.Process(target=left_branch, args=(subject, result_queue))
    process2 = multiprocessing.Process(target=right_branch, args=(subject, exam_type, user_id, result_queue))

    process1.start()
    process2.start()

    process1.join()
    process2.join()

    # Collect results from both branches
    branch_errors = {}
    while not result_queue.empty():
        branch_name, err_type = result_queue.get_nowait()
        branch_errors[branch_name] = err_type

    left_error = branch_errors.get("left")
    right_error = branch_errors.get("right")

    if left_error is not None or right_error is not None or process1.exitcode != 0 or process2.exitcode != 0:
        print("!!! One or more branches failed. Aborting orchestration. !!!", flush=True)

        # Determine dominant error type: rate_limit > network > logical
        dominant_error = "logical"
        for err in [left_error, right_error]:
            if err == "rate_limit":
                dominant_error = "rate_limit"
                break
            elif err == "network" and dominant_error != "rate_limit":
                dominant_error = "network"

        if dominant_error == "rate_limit":
            sys.exit(EXIT_RATE_LIMIT)
        elif dominant_error == "network":
            sys.exit(EXIT_NETWORK)
        else:
            sys.exit(EXIT_LOGICAL)

    print("--- Both branches have finished execution ---", flush=True)

    # 3. Run the final clustering script
    success, error_type = run_script("agglomerative_clustering.py", subject)
    if not success:
        print(f"!!! Clustering script failed. Error type: {error_type} !!!", flush=True)
        if error_type == "rate_limit":
            sys.exit(EXIT_RATE_LIMIT)
        elif error_type == "network":
            sys.exit(EXIT_NETWORK)
        else:
            sys.exit(EXIT_LOGICAL)

    print(">>> Orchestrator Finished <<<", flush=True)

    end_time = time.time()
    total_time = end_time - start_time
    print(f"Total execution time: {total_time:.2f} seconds")
