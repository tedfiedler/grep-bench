#!/usr/bin/env bash
# Build the three images, generate the encrypted log data into a named
# volume (once), benchmark each container, and produce report.md.
set -euo pipefail
cd "$(dirname "$0")"

FILES=${FILES:-5}
GB=${GB:-1}
VOLUME=${VOLUME:-grepbench-data}

echo "== Building images =="
docker build -q -f Dockerfile.alpine-busybox  -t grepbench-alpine-busybox  .
docker build -q -f Dockerfile.debian-hardened -t grepbench-debian-hardened .
docker build -q -f Dockerfile.alpine-gnugrep  -t grepbench-alpine-gnugrep  .

docker volume create "$VOLUME" >/dev/null

echo "== Generating data (skipped if already present) =="
if ! docker run --rm -v "$VOLUME":/data grepbench-alpine-busybox test -f /data/uuids.txt; then
  docker run --rm -u 0 -v "$VOLUME":/data grepbench-debian-hardened \
    python /app/generate_logs.py --out /data --files "$FILES" --gb "$GB"
fi

mkdir -p results

# Drop the Linux VM's page cache so every container starts cold.
drop_caches() {
  docker run --rm --privileged grepbench-alpine-busybox \
    sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
}

for name in alpine-busybox debian-hardened alpine-gnugrep; do
  echo "== Benchmarking $name =="
  drop_caches
  docker run --rm \
    --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp \
    -v "$VOLUME":/data:ro -v "$PWD/results":/results \
    "grepbench-$name" python /app/bench.py --name "$name"
done

echo "== Strategy comparison (grep vs rg vs pure Python vs index) =="
if ! docker run --rm -v "$VOLUME":/data grepbench-alpine-busybox test -f /data/index.db; then
  docker run --rm -u 0 -v "$VOLUME":/data grepbench-debian-hardened \
    python /app/build_index.py --data-dir /data
fi
for name in debian-hardened alpine-gnugrep; do
  docker run --rm \
    --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp \
    -v "$VOLUME":/data:ro -v "$PWD/results":/results \
    "grepbench-$name" python /app/bench_strategies.py --label "$name"
done

echo "== Report =="
python3 report.py results/*.json | tee report.md
