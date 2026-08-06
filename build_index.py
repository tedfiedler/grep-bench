#!/usr/bin/env python3
"""Build a UUID -> line-offset index over the log files in one scan.

Stores one row per UUID in SQLite: the value is a packed array of
(file_idx: u8, byte_offset: u64) little-endian pairs, one per matching line.
File index refers to sorted(logs_*.jsonl) order.
"""
import argparse
import glob
import os
import sqlite3
import struct
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="/data")
    ap.add_argument("--out", default=None, help="default: <data-dir>/index.db")
    args = ap.parse_args()
    out = args.out or os.path.join(args.data_dir, "index.db")

    files = sorted(glob.glob(os.path.join(args.data_dir, "logs_*.jsonl")))
    t0 = time.perf_counter()
    index = {}
    total_lines = 0
    for fi, path in enumerate(files):
        offset = 0
        with open(path, "rb") as f:
            for line in f:
                p = line.find(b'"id":"')
                uid = line[p + 6 : p + 42]
                index.setdefault(uid, bytearray()).extend(struct.pack("<BQ", fi, offset))
                offset += len(line)
                total_lines += 1
        print(f"[index] scanned {path}", flush=True)
    scan_s = time.perf_counter() - t0

    tmp = out + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE idx (uuid TEXT PRIMARY KEY, positions BLOB)")
    con.executemany(
        "INSERT INTO idx VALUES (?, ?)",
        ((k.decode(), bytes(v)) for k, v in index.items()),
    )
    con.commit()
    con.close()
    os.replace(tmp, out)

    total_s = time.perf_counter() - t0
    print(f"[index] {total_lines:,} lines, {len(index):,} uuids -> {out} "
          f"({os.path.getsize(out) / 1e6:.0f} MB) in {total_s:.1f}s "
          f"(scan {scan_s:.1f}s)", flush=True)


if __name__ == "__main__":
    main()
