# grep container benchmark

Generated 2026-08-05 20:05 · 5 files · 5.03 GB total · arch aarch64

Task: Python app runs `subprocess` → `grep -F -h <uuid> logs_*.jsonl` over all files, then AES-256-GCM-decrypts every matched message. Same seeded UUID set in every container. Rep 0 starts against a dropped VM page cache; warm stats cover the remaining reps.

| Container | grep | Cold first grep | Warm grep mean | Warm min | Throughput | Decrypt mean | Lines/query | Total/query | vs fastest |
|---|---|---|---|---|---|---|---|---|---|
| debian-hardened | grep (GNU grep) 3.8 | 2.91 s | 0.73 ± 0.02 s | 0.71 s | 6.90 GB/s | 1.6 ms | 732 | 0.73 s | — |
| alpine-gnugrep | grep (GNU grep) 3.12 | 2.61 s | 0.87 ± 0.01 s | 0.85 s | 5.79 GB/s | 2.0 ms | 732 | 0.87 s | 1.2× slower |
| alpine-busybox | grep (BusyBox v1.37.0) | 38.27 s | 36.38 ± 0.21 s | 36.12 s | 0.14 GB/s | 2.1 ms | 732 | 36.38 s | 49.9× slower |

**Fastest:** debian-hardened at 0.73 s per query (6.90 GB/s). Decryption of ~732 matched lines costs ~2 ms per query — grep dominates the end-to-end time in every container.
