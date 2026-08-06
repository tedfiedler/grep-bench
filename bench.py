#!/usr/bin/env python3
"""Run the UUID search benchmark inside a container and write results JSON.

The same seeded sample of UUIDs is used in every container so all three
search for identical targets. Rep 0 runs against a freshly dropped page
cache (the host script drops the VM cache before starting the container)
and is reported separately; reps >= 1 measure warm-cache performance.
"""
import argparse
import json
import os
import platform
import random
import subprocess
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from search import decrypt_lines, find_lines, log_files


def grep_version():
    path = "unknown"
    try:
        which = subprocess.run(["which", "grep"], capture_output=True, text=True)
        path = os.path.realpath(which.stdout.strip()) if which.stdout.strip() else "unknown"
    except OSError:
        pass
    try:
        if "busybox" in path:
            p = subprocess.run(["busybox"], capture_output=True, text=True)
            first = (p.stdout + p.stderr).splitlines()[0]
            return f"grep ({first.split('(')[0].strip()})", path
        p = subprocess.run(["grep", "--version"], capture_output=True, text=True)
        for line in (p.stdout + p.stderr).splitlines():
            if line.lower().startswith("grep ("):
                return line.strip(), path
    except (OSError, IndexError):
        pass
    return "unknown", path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--data-dir", default="/data")
    ap.add_argument("--key-file", default="/data/key.hex")
    ap.add_argument("--results-dir", default="/results")
    ap.add_argument("--queries", type=int, default=5)
    ap.add_argument("--reps", type=int, default=2)
    args = ap.parse_args()

    files = log_files(args.data_dir)
    total_bytes = sum(os.path.getsize(f) for f in files)
    aes = AESGCM(bytes.fromhex(open(args.key_file).read().strip()))

    uuids = open(os.path.join(args.data_dir, "uuids.txt")).read().split()
    queries = random.Random(4242).sample(uuids, args.queries)

    version, grep_path = grep_version()
    results = {
        "name": args.name,
        "grep_version": version,
        "grep_path": grep_path,
        "machine": platform.machine(),
        "python": platform.python_version(),
        "files": len(files),
        "total_bytes": total_bytes,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runs": [],
    }
    print(f"[{args.name}] grep: {version} ({grep_path}), "
          f"{len(files)} files, {total_bytes / 1e9:.2f} GB", flush=True)

    for rep in range(args.reps + 1):
        for q in queries:
            lines, grep_s = find_lines(q, files)
            records, decrypt_s = decrypt_lines(lines, aes)
            assert len(records) == len(lines)
            results["runs"].append(
                {
                    "uuid": q,
                    "rep": rep,
                    "cold": rep == 0,
                    "grep_s": round(grep_s, 4),
                    "decrypt_s": round(decrypt_s, 4),
                    "lines": len(lines),
                }
            )
            print(f"[{args.name}] rep{rep} {q[:8]} grep={grep_s:6.2f}s "
                  f"decrypt={decrypt_s:.3f}s lines={len(lines)}", flush=True)

    out_path = os.path.join(args.results_dir, f"{args.name}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"[{args.name}] wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
