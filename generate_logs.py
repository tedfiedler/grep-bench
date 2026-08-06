#!/usr/bin/env python3
"""Generate encrypted JSONL log files for the grep benchmark.

Each line is a JSON object with an "id" field (a UUID shared by 500-1000
lines) and a "msg" field containing base64(nonce || AES-256-GCM ciphertext).
Files are written in parallel, one process per file. The AES key is stored
as hex in <out>/key.hex and the full list of generated UUIDs in
<out>/uuids.txt.
"""
import argparse
import base64
import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from multiprocessing import Process

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

LEVELS = ["DEBUG", "INFO", "INFO", "INFO", "INFO", "WARN", "ERROR"]
SERVICES = ["auth", "api", "worker", "scheduler", "payments", "ingest", "gateway"]
TEMPLATES = [
    "request completed",
    "cache miss for key",
    "retrying upstream call",
    "user session refreshed",
    "queue depth sampled",
    "db query executed",
    "webhook delivered",
    "rate limit consulted",
]

ACTIVE_UUIDS = 250        # uuids interleaved at any point in a file
MIN_LINES, MAX_LINES = 500, 1000
CHECK_EVERY = 20000       # lines between file-size checks


def make_plaintext(rng):
    pad = rng.randint(0, 100)
    return (
        f"{rng.choice(TEMPLATES)} status={rng.randint(200, 599)} "
        f"duration_ms={rng.randint(1, 9000)} trace={os.urandom(8).hex()} "
        f"detail={os.urandom(pad).hex()}"
    )


def write_file(idx, out_dir, target_bytes, key_hex, seed):
    rng = random.Random(seed)
    aes = AESGCM(bytes.fromhex(key_hex))
    path = os.path.join(out_dir, f"logs_{idx:02d}.jsonl")
    uuid_part = os.path.join(out_dir, f".uuids_{idx:02d}.txt")
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=idx * 500)

    with open(path, "w") as f, open(uuid_part, "w") as uf:

        def new_entry():
            uid = str(uuid.uuid4())
            uf.write(uid + "\n")
            return [uid, rng.randint(MIN_LINES, MAX_LINES)]

        pool = [new_entry() for _ in range(ACTIVE_UUIDS)]
        lines = 0
        while True:
            i = rng.randrange(ACTIVE_UUIDS)
            uid = pool[i][0]
            pool[i][1] -= 1
            if pool[i][1] <= 0:
                pool[i] = new_entry()

            ts += timedelta(milliseconds=rng.randint(1, 50))
            nonce = os.urandom(12)
            ct = aes.encrypt(nonce, make_plaintext(rng).encode(), None)
            rec = {
                "ts": ts.isoformat(timespec="milliseconds"),
                "level": rng.choice(LEVELS),
                "service": rng.choice(SERVICES),
                "id": uid,
                "msg": base64.b64encode(nonce + ct).decode(),
            }
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            lines += 1
            if lines % CHECK_EVERY == 0 and f.tell() >= target_bytes:
                break

    print(f"[gen] {path}: {lines:,} lines, {os.path.getsize(path) / 1e9:.2f} GB", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--files", type=int, default=5)
    ap.add_argument("--gb", type=float, default=1.0)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    key_path = os.path.join(args.out, "key.hex")
    if os.path.exists(key_path):
        key_hex = open(key_path).read().strip()
    else:
        key_hex = os.urandom(32).hex()
        with open(key_path, "w") as f:
            f.write(key_hex + "\n")

    target = int(args.gb * 1_000_000_000)
    procs = [
        Process(target=write_file, args=(i, args.out, target, key_hex, 1000 + i))
        for i in range(args.files)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        if p.exitcode != 0:
            raise SystemExit(f"generator process failed with exit code {p.exitcode}")

    with open(os.path.join(args.out, "uuids.txt"), "w") as out:
        for i in range(args.files):
            part = os.path.join(args.out, f".uuids_{i:02d}.txt")
            out.write(open(part).read())
            os.remove(part)
    print("[gen] done", flush=True)


if __name__ == "__main__":
    main()
