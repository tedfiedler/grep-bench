#!/usr/bin/env python3
"""Compare UUID-search strategies against the grep-subprocess baseline.

Strategies:
  grep      subprocess grep -F -h (the original approach; control)
  rg        subprocess ripgrep -F -I -N (skipped if not installed)
  mmap      pure Python: mmap each file, bytes.find loop, sequential
  mmap-par  same, one process per file
  index     SQLite UUID -> (file, offset) index + seek/readline
            (build once with build_index.py; skipped if index.db is missing)

All strategies must return the same number of lines per UUID or the run
aborts. Timings are find-phase only; decryption adds ~2 ms regardless.
"""
import argparse
import glob
import json
import mmap
import os
import random
import shutil
import sqlite3
import statistics
import struct
import subprocess
import time
from multiprocessing import Pool


def scan_file(args):
    path, needle = args
    hits = []
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
        pos = mm.find(needle)
        while pos != -1:
            start = mm.rfind(b"\n", max(0, pos - 2000), pos) + 1
            end = mm.find(b"\n", pos)
            if end == -1:
                end = len(mm)
            hits.append(mm[start:end])
            pos = mm.find(needle, end)
        mm.close()
    return hits


def find_mmap(uuid, files):
    lines = []
    for path in files:
        lines.extend(scan_file((path, uuid.encode())))
    return lines


def find_mmap_par(uuid, files, pool):
    out = pool.map(scan_file, [(p, uuid.encode()) for p in files])
    return [line for hits in out for line in hits]


def find_subprocess(argv, files, uuid):
    proc = subprocess.run(argv + [uuid] + files, capture_output=True)
    if proc.returncode > 1:
        raise RuntimeError(proc.stderr.decode().strip())
    return proc.stdout.splitlines()


def find_index(uuid, handles, con):
    row = con.execute("SELECT positions FROM idx WHERE uuid = ?", (uuid,)).fetchone()
    if row is None:
        return []
    lines = []
    for fi, offset in struct.iter_unpack("<BQ", row[0]):
        f = handles[fi]
        f.seek(offset)
        lines.append(f.readline().rstrip(b"\n"))
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--data-dir", default="/data")
    ap.add_argument("--results-dir", default="/results")
    ap.add_argument("--queries", type=int, default=5)
    ap.add_argument("--reps", type=int, default=2)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.data_dir, "logs_*.jsonl")))
    uuids = open(os.path.join(args.data_dir, "uuids.txt")).read().split()
    queries = random.Random(4242).sample(uuids, args.queries)

    pool = Pool(len(files))
    strategies = [
        ("grep", lambda q: find_subprocess(["grep", "-F", "-h"], files, q)),
        ("mmap", lambda q: find_mmap(q, files)),
        ("mmap-par", lambda q: find_mmap_par(q, files, pool)),
    ]
    if shutil.which("rg"):
        strategies.insert(1, ("rg", lambda q: find_subprocess(["rg", "-F", "-I", "-N"], files, q)))
    index_path = os.path.join(args.data_dir, "index.db")
    if os.path.exists(index_path):
        con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        handles = [open(p, "rb") for p in files]
        strategies.append(("index", lambda q: find_index(q, handles, con)))

    # warm the page cache once so every strategy is measured warm
    find_subprocess(["grep", "-F", "-h"], files, queries[0])

    expected = {}
    out = {"label": args.label, "queries": args.queries, "reps": args.reps,
           "total_bytes": sum(os.path.getsize(f) for f in files), "strategies": {}}
    for name, fn in strategies:
        times = []
        for _ in range(args.reps):
            for q in queries:
                t0 = time.perf_counter()
                lines = fn(q)
                times.append(time.perf_counter() - t0)
                expected.setdefault(q, len(lines))
                if len(lines) != expected[q]:
                    raise SystemExit(f"{name}: {q} matched {len(lines)}, expected {expected[q]}")
        out["strategies"][name] = {
            "mean_s": round(statistics.mean(times), 4),
            "min_s": round(min(times), 4),
            "max_s": round(max(times), 4),
            "runs": [round(t, 4) for t in times],
        }
        print(f"[{args.label}] {name:9s} mean={statistics.mean(times):6.3f}s "
              f"min={min(times):6.3f}s max={max(times):6.3f}s  "
              f"({len(times)} runs, results verified)", flush=True)
    pool.close()

    path = os.path.join(args.results_dir, f"strategies-{args.label}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"[{args.label}] wrote {path}", flush=True)


if __name__ == "__main__":
    main()
