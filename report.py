#!/usr/bin/env python3
"""Merge per-container bench results into a markdown report. Stdlib only."""
import json
import statistics
import sys
import time


def load(paths):
    # skip strategies-*.json and anything else without per-run container data
    return [d for d in (json.load(open(p)) for p in paths) if "runs" in d]


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: report.py results/*.json")
    data = load(sys.argv[1:])

    gb = data[0]["total_bytes"] / 1e9
    print("# grep container benchmark")
    print()
    print(f"Generated {time.strftime('%Y-%m-%d %H:%M')} · "
          f"{data[0]['files']} files · {gb:.2f} GB total · arch {data[0]['machine']}")
    print()
    print("Task: Python app runs `subprocess` → `grep -F -h <uuid> logs_*.jsonl` "
          "over all files, then AES-256-GCM-decrypts every matched message. "
          "Same seeded UUID set in every container. Rep 0 starts against a "
          "dropped VM page cache; warm stats cover the remaining reps.")
    print()

    rows = []
    for d in data:
        warm = [r for r in d["runs"] if not r["cold"]]
        cold_first = d["runs"][0]  # first query after the cache drop
        grep_times = [r["grep_s"] for r in warm]
        rows.append(
            {
                "name": d["name"],
                "grep": d["grep_version"],
                "cold_s": cold_first["grep_s"],
                "warm_mean": statistics.mean(grep_times),
                "warm_stdev": statistics.stdev(grep_times) if len(grep_times) > 1 else 0.0,
                "warm_min": min(grep_times),
                "tput": d["total_bytes"] / 1e9 / statistics.mean(grep_times),
                "dec_mean": statistics.mean(r["decrypt_s"] for r in warm),
                "lines_mean": statistics.mean(r["lines"] for r in warm),
                "total_mean": statistics.mean(r["grep_s"] + r["decrypt_s"] for r in warm),
            }
        )

    rows.sort(key=lambda r: r["warm_mean"])
    fastest = rows[0]["warm_mean"]

    print("| Container | grep | Cold first grep | Warm grep mean | Warm min | Throughput | Decrypt mean | Lines/query | Total/query | vs fastest |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        rel = r["warm_mean"] / fastest
        print(
            f"| {r['name']} | {r['grep']} | {r['cold_s']:.2f} s | "
            f"{r['warm_mean']:.2f} ± {r['warm_stdev']:.2f} s | {r['warm_min']:.2f} s | "
            f"{r['tput']:.2f} GB/s | {r['dec_mean'] * 1000:.1f} ms | "
            f"{r['lines_mean']:.0f} | {r['total_mean']:.2f} s | "
            f"{'—' if rel < 1.005 else f'{rel:.1f}× slower'} |"
        )

    print()
    print(f"**Fastest:** {rows[0]['name']} at {rows[0]['warm_mean']:.2f} s per query "
          f"({rows[0]['tput']:.2f} GB/s). Decryption of ~{rows[0]['lines_mean']:.0f} "
          f"matched lines costs ~{rows[0]['dec_mean'] * 1000:.0f} ms per query — "
          "grep dominates the end-to-end time in every container.")


if __name__ == "__main__":
    main()
