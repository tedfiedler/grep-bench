#!/usr/bin/env python3
"""Find all log lines for a UUID via subprocess grep, then decrypt the messages.

This is the application under test: the grep phase and the decrypt phase are
timed separately.
"""
import argparse
import base64
import glob
import json
import os
import subprocess
import sys
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def log_files(data_dir):
    return sorted(glob.glob(os.path.join(data_dir, "logs_*.jsonl")))


def find_lines(target, files):
    """Run grep -F -h <target> over the files. Returns (lines, seconds)."""
    t0 = time.perf_counter()
    proc = subprocess.run(["grep", "-F", "-h", target] + files, capture_output=True)
    elapsed = time.perf_counter() - t0
    if proc.returncode > 1:  # 1 just means "no matches"
        raise RuntimeError(f"grep failed: {proc.stderr.decode().strip()}")
    return proc.stdout.decode().splitlines(), elapsed


def decrypt_lines(lines, aes):
    """Parse JSON lines and decrypt msg fields. Returns (records, seconds)."""
    t0 = time.perf_counter()
    records = []
    for line in lines:
        rec = json.loads(line)
        raw = base64.b64decode(rec["msg"])
        rec["msg"] = aes.decrypt(raw[:12], raw[12:], None).decode()
        records.append(rec)
    return records, time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uuid")
    ap.add_argument("--data-dir", default="/data")
    ap.add_argument("--key-file", default="/data/key.hex")
    ap.add_argument("--quiet", action="store_true", help="skip printing the decrypted lines")
    ap.add_argument("--stats", action="store_true", help="print timing JSON to stderr")
    args = ap.parse_args()

    files = log_files(args.data_dir)
    if not files:
        raise SystemExit(f"no logs_*.jsonl files in {args.data_dir}")
    aes = AESGCM(bytes.fromhex(open(args.key_file).read().strip()))

    lines, grep_s = find_lines(args.uuid, files)
    records, decrypt_s = decrypt_lines(lines, aes)

    if not args.quiet:
        for r in records:
            print(f'{r["ts"]} {r["level"]:5s} {r["service"]:9s} {r["id"]} {r["msg"]}')

    if args.stats:
        json.dump(
            {
                "uuid": args.uuid,
                "lines": len(records),
                "grep_s": round(grep_s, 4),
                "decrypt_s": round(decrypt_s, 4),
                "total_s": round(grep_s + decrypt_s, 4),
            },
            sys.stderr,
        )
        sys.stderr.write("\n")


if __name__ == "__main__":
    main()
