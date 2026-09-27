"""
Loads sample_events.json into a running instance of the service via
POST /events/batch, in chunks (so a single oversized request body isn't
required). Stdlib-only (urllib) so it needs no extra dependency to run.

Usage:
    python scripts/load_sample_data.py [--base-url http://localhost:8000] [--chunk-size 500]
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def post_batch(base_url: str, api_key: str, chunk: list[dict]) -> dict:
    url = f"{base_url.rstrip('/')}/events/batch"
    data = json.dumps(chunk).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--file", default=str(Path(__file__).resolve().parent.parent / "sample_events.json"))
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()

    events = json.loads(Path(args.file).read_text())
    total_accepted = total_duplicate = total_rejected = 0

    for i in range(0, len(events), args.chunk_size):
        chunk = events[i : i + args.chunk_size]
        try:
            result = post_batch(args.base_url, args.api_key, chunk)
        except urllib.error.URLError as exc:
            print(f"Chunk starting at {i} failed: {exc}")
            raise SystemExit(1)

        total_accepted += result["accepted_count"]
        total_duplicate += result["duplicate_count"]
        total_rejected += result["rejected_count"]
        print(
            f"chunk {i // args.chunk_size + 1}: "
            f"accepted={result['accepted_count']} "
            f"duplicate={result['duplicate_count']} "
            f"rejected={result['rejected_count']}"
        )
        if result["rejected_count"]:
            print("  rejected sample:", result["rejected"][:2])
        time.sleep(0.05)  # be gentle on the deployed instance

    print(
        f"\nDone. total_accepted={total_accepted} "
        f"total_duplicate={total_duplicate} total_rejected={total_rejected}"
    )


if __name__ == "__main__":
    main()
