# grep-bench

How much does the grep binary inside your container matter? A benchmark of a
Python app that shells out to `grep` to find UUIDs in ~5 GB of encrypted JSON
logs, run across three Docker containers — and then a comparison against
faster alternatives.

## Results

Warm page cache, mean per query over 5.03 GB (12.5M lines, ~732 matches per
UUID), Docker Desktop on an Apple Silicon Mac (aarch64 VM, 8 vCPU):

| Container | grep | Warm mean/query | vs fastest |
|---|---|---|---|
| debian-hardened (glibc) | GNU grep 3.8 | 0.73 s | — |
| alpine-gnugrep (musl) | GNU grep 3.12 | 0.87 s | 1.2× |
| alpine-busybox | BusyBox 1.37.0 | 36.4 s | **50×** |

BusyBox grep is a naive matcher and purely CPU-bound — `apk add grep` recovers
~97% of the gap with zero code changes. The residual glibc-vs-musl difference
(~20%) persists even though Alpine ships the newer grep.

But grep isn't the ceiling:

| Strategy | Debian/glibc | Alpine/musl |
|---|---|---|
| `grep -F` subprocess | 0.73 s | 0.87 s |
| `rg -F` subprocess (rg 13 / 15) | 0.42 s | 0.11 s |
| Python `mmap` + `bytes.find`, sequential | 0.71 s | 0.69 s |
| Python `mmap`, one worker per file | 0.16 s | 0.16 s |
| SQLite UUID→offset index + seek | ~1 ms | ~1 ms |

Pure Python matches GNU grep (CPython's substring search is C-level, and
libc-independent — it fixes stock Alpine without installing anything),
parallelism across files buys ~5×, and if lookups recur, a one-time index
(5.6 s to build, 126 MB) beats every scanning strategy by ~500×. The index
design — including how to run it live against logs written by many
applications — is documented in detail in [INDEXING.md](INDEXING.md).

Decrypting the ~732 matched messages (AES-256-GCM) costs ~2 ms — noise.

## Layout

- `generate_logs.py` — writes N×1 GB JSONL files; each line has an `id` UUID
  (recurring on 500–1000 interleaved lines) and `msg` =
  base64(nonce ‖ AES-256-GCM ciphertext)
- `search.py` — the app under test: `subprocess` → `grep -F -h`, then decrypt
- `pygrep.py` — drop-in pure-Python replacement (parallel mmap scan), no grep
  binary needed
- `bench.py` / `bench_strategies.py` — per-container and per-strategy timing
- `build_index.py` — one-pass UUID → (file, byte-offset) SQLite index
- `live_indexer.py` — incremental version of that index for live, multi-app
  logs (watermark tailing, rotation handling, WAL, single-writer lock);
  tested by `test_live_indexer.py` (`python3 -m unittest test_live_indexer`)
- `Dockerfile.*` — the three container variants
- `run_bench.sh` — end-to-end: build, generate (once, into a named volume),
  benchmark with page-cache drops, report
- `report.py` → `report.md`; `report.html` — the full report with charts;
  `results/` — raw timings

## Run it

Requires Docker (data generation needs ~5 GB in a named volume) and takes
~25 minutes on the first run, most of it BusyBox grep being slow:

```sh
./run_bench.sh
```

`FILES=2 GB=0.1 VOLUME=grepbench-small ./run_bench.sh` runs a quick smoke test.

## Caveats

Absolute numbers are from Docker's Linux VM on macOS; the relative rankings
are the durable result. "Hardened Debian" here means slim-bookworm with a
non-root user, stripped setuid/setgid bits, dropped capabilities, and a
read-only rootfs — all three containers run with the same runtime flags.
