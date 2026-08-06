# UUID → offset indexing with SQLite

This documents the indexing strategy that turned a 0.7-second grep over 5 GB
into a ~1 ms lookup, as measured in this repo — first the batch process the
benchmark actually ran (`build_index.py` + the `index` strategy in
`bench_strategies.py`), then how to extend the same design to a live system
where many applications are writing logs concurrently.

The core idea: **the log files stay the source of truth; SQLite stores only
_where to look_.** The index maps each UUID to the byte positions of its lines
inside the files. A lookup is: one SQL query, then a seek + read per matching
line, then decrypt. Nothing is duplicated, the index is derived data you can
throw away and rebuild, and the encrypted payloads never enter the database.

```mermaid
flowchart LR
    subgraph apps [Log producers]
        A1[app 1] --> F
        A2[app 2] --> F
        A3[app N] --> F
    end
    F[(JSONL log files\nsource of truth)]
    F -- "scan once /\ntail incrementally" --> IDX[indexer process\n(the only SQLite writer)]
    IDX --> DB[(index.db\nuuid → file, offset)]
    DB -- "1. SELECT positions" --> R[lookup tool]
    F -- "2. seek + readline" --> R
    R -- "3. AES-GCM decrypt" --> OUT[matching log lines]
```

## Measured results (this repo's dataset)

5 files × 1.01 GB, 12,500,000 lines, 17,339 distinct UUIDs, ~732 lines per
UUID, Docker Desktop aarch64 VM (8 vCPU):

| Step | Cost |
|---|---|
| Build index (full scan, batch) | 5.6 s |
| Index size on disk | 126 MB (~10 bytes per indexed line, ~2.5% of data) |
| Lookup, warm cache | ~1.4 ms mean (0.8–4 ms observed) |
| Same lookup via GNU grep | ~730 ms |
| Same lookup via BusyBox grep | ~36 s |

The lookup returns byte-identical lines to grep — the harness verifies every
strategy against the same match counts.

---

## Part 1 — The batch process (what the benchmark ran)

### 1.1 Log format assumptions

One JSON object per line ("JSONL"). Each line contains an `"id"` field whose
value is a 36-character UUID, e.g.:

```json
{"ts":"2026-03-05T04:27:06.815+00:00","level":"ERROR","service":"payments","id":"214c5d0c-a2f9-4eac-89e2-ef943194fef4","msg":"<base64 nonce‖ciphertext‖tag>"}
```

The indexer does **not** JSON-parse during the scan (too slow at 12.5M lines).
It relies on two structural facts:

- the byte pattern `"id":"` appears exactly once per line, immediately before
  the UUID (`generate_logs.py` emits compact JSON with a fixed key order);
- lines are newline-terminated, so a byte offset + `readline()` recovers a
  whole record.

If your producers can't guarantee `"id":"` appears once and only before the
UUID (e.g. free-text fields could contain that substring), swap the
`line.find(b'"id":"')` extraction for a real `json.loads` — the build gets
several times slower but stays correct, and lookups are unaffected.

### 1.2 Schema

```sql
CREATE TABLE idx (
    uuid      TEXT PRIMARY KEY,   -- the 36-char UUID
    positions BLOB                -- packed array of (file_idx, offset) pairs
);
```

`positions` is a packed binary array: repeating 9-byte records of
`struct` format `<BQ` — one unsigned byte for the file index, one
little-endian unsigned 64-bit byte offset. A UUID with 732 lines is one row
with a 6,588-byte blob.

**Why a packed blob instead of one row per line?** At this scale it's the
difference between 17k rows and 12.5M rows:

| | packed blob (this design) | row per line |
|---|---|---|
| Rows | 17,339 | 12,500,000 |
| Build insert cost | one `executemany` pass, seconds | minutes of B-tree churn |
| Size | 126 MB | ~500 MB+ (rowid + key repeated per line) |
| Lookup | 1 query, decode in Python | 1 query, ~732 rows |
| Incremental append | awkward (blob rewrite) | natural (`INSERT`) |

The last row of that table matters: the packed blob is the right call for
**batch** builds over immutable files, and the wrong call for **live** append —
Part 2 changes the schema for that reason.

`file_idx` refers to position in `sorted(glob("logs_*.jsonl"))`. That sort
order is part of the index's contract: lookups must glob and sort the same
way. Zero-padded names (`logs_00` … `logs_04`) keep the order stable, and new
files whose names sort after the existing ones don't disturb old entries.

### 1.3 Build (`build_index.py`)

The whole build is one sequential pass per file:

```python
index = {}                                    # uuid bytes -> bytearray of packed pairs
for fi, path in enumerate(files):             # files = sorted(glob(...))
    offset = 0
    with open(path, "rb") as f:
        for line in f:                        # buffered, ~2.3M lines/s
            p = line.find(b'"id":"')
            uid = line[p + 6 : p + 42]        # 36-byte UUID
            index.setdefault(uid, bytearray()).extend(struct.pack("<BQ", fi, offset))
            offset += len(line)
```

Notes on the details:

- **Binary mode** (`"rb"`): offsets are byte offsets, and text mode would
  corrupt them (and be slower).
- **Offsets are accumulated, not `f.tell()`-ed**: calling `tell()` per line
  defeats read buffering; summing `len(line)` is free and exact.
- **Memory**: the dict holds ~9 bytes per line plus overhead — ~150 MB for
  12.5M lines. For datasets an order of magnitude larger, flush per-file
  partial results to SQLite instead of holding everything.
- **Write is atomic**: the table is written to `index.db.tmp` and swapped in
  with `os.replace()`, so a reader never sees a half-built index and a crashed
  build leaves the previous index intact:

```python
con = sqlite3.connect(out + ".tmp")
con.execute("CREATE TABLE idx (uuid TEXT PRIMARY KEY, positions BLOB)")
con.executemany("INSERT INTO idx VALUES (?, ?)",
                ((k.decode(), bytes(v)) for k, v in index.items()))
con.commit(); con.close()
os.replace(out + ".tmp", out)                 # atomic on POSIX
```

Run it (in this repo, against the benchmark volume):

```sh
docker run --rm -u 0 -v grepbench-data:/data grepbench-debian-hardened \
  python /app/build_index.py --data-dir /data
```

### 1.4 Lookup (the `index` strategy in `bench_strategies.py`)

```python
con = sqlite3.connect("file:/data/index.db?mode=ro", uri=True)
handles = [open(p, "rb") for p in files]      # open once, reuse across lookups

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
```

- **`mode=ro`** opens the database read-only — a lookup tool can never
  accidentally take a write lock.
- **File handles are opened once** and reused; per-lookup cost is one B-tree
  probe plus ~732 `seek`+`readline` pairs. Warm page cache makes each read a
  few microseconds; a cold cache costs one disk read per line (still tens of
  milliseconds, not seconds).
- Decryption happens after retrieval, exactly as in `search.py` — the index
  never sees plaintext.

### 1.5 Correctness checks worth keeping

- **Count parity with grep**: `bench_strategies.py` asserts every strategy
  returns the same number of lines per UUID. Cheap and catches offset bugs
  immediately — keep an equivalent spot check in any port of this design.
- **Rebuild is the recovery story**: at 5.6 s per 5 GB, "delete index.db and
  rebuild" is a viable answer to corruption, schema changes, or doubt. Treat
  the index as a cache; never treat it as data.

---

## Part 2 — Extending to live logs from many applications

The batch design assumes immutable files. In production, applications append
continuously. The key architectural fact that keeps SQLite viable:

> **Many applications writing logs ≠ many writers to the index.**
> Apps append to log files exactly as they do today. One indexer process
> tails those files and is the *only* process that writes to SQLite.
> SQLite's single-writer constraint is then never contended, and WAL mode
> lets any number of lookup processes read concurrently while the indexer
> writes.

### 2.1 Schema changes for append

Blob-per-UUID can't be appended to cheaply (SQLite blobs aren't growable in
place via SQL), so the live schema goes row-per-line, which SQLite handles
fine at incremental rates:

```sql
PRAGMA journal_mode = WAL;          -- readers don't block the writer, and vice versa
PRAGMA synchronous  = NORMAL;       -- fsync at checkpoint, not every commit; safe with WAL

CREATE TABLE files (
    file_idx    INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    inode       INTEGER NOT NULL,   -- rotation detection
    indexed_to  INTEGER NOT NULL    -- byte watermark: everything before this is indexed
);

CREATE TABLE idx (
    uuid     TEXT    NOT NULL,
    file_idx INTEGER NOT NULL,
    offset   INTEGER NOT NULL
);
CREATE INDEX idx_uuid ON idx(uuid);
```

The `files` table replaces the "sorted glob order" contract from Part 1 —
file identity is now explicit, so rotation and new files can't silently
renumber anything.

### 2.2 The indexer loop

This design is implemented in [`live_indexer.py`](live_indexer.py) and tested
in [`test_live_indexer.py`](test_live_indexer.py) (stdlib-only:
`python3 -m unittest test_live_indexer`). The snippet below shows the shape
of one polling cycle (the polling mechanism itself is swappable —
inotify/FSEvents work too); the real implementation adds the full
reconciliation: rename, truncation, and deletion handling, keyed by inode
rather than path (so it drops the `UNIQUE(path)` constraint shown above —
the inode is the identity, the path is just its current location). It also
exposes observability: every cycle produces a metrics dict (cycle duration,
lines indexed, per-file unindexed byte lag) available as
`indexer.last_metrics`, delivered to an optional `on_cycle` callback for
wiring into statsd/Prometheus (a failing sink never interrupts indexing),
and printable as JSON lines via `--metrics` on the CLI. `lag_bytes_total`
is the number to alert on — a healthy indexer returns it to 0 every cycle:

```python
def index_cycle(con, log_dir):
    for path in sorted(glob.glob(os.path.join(log_dir, "*.jsonl"))):
        st = os.stat(path)
        row = con.execute("SELECT file_idx, inode, indexed_to FROM files WHERE path = ?",
                          (path,)).fetchone()
        if row is None or row[1] != st.st_ino:        # new file, or rotated in place
            cur = con.execute(
                "INSERT INTO files (path, inode, indexed_to) VALUES (?, ?, 0) "
                "ON CONFLICT(path) DO UPDATE SET inode = excluded.inode, indexed_to = 0 "
                "RETURNING file_idx", (path, st.st_ino))
            file_idx, watermark = cur.fetchone()[0], 0
        else:
            file_idx, _, watermark = row
        if st.st_size <= watermark:
            continue                                   # nothing new

        with open(path, "rb") as f:
            f.seek(watermark)
            batch, offset = [], watermark
            for line in f:
                if not line.endswith(b"\n"):
                    break                              # partial line mid-write: wait for next cycle
                p = line.find(b'"id":"')
                if p != -1:
                    batch.append((line[p + 6 : p + 42].decode(), file_idx, offset))
                offset += len(line)

        # one transaction: rows + watermark move together, so a crash
        # between cycles can never double-index or skip lines
        with con:
            con.executemany("INSERT INTO idx VALUES (?, ?, ?)", batch)
            con.execute("UPDATE files SET indexed_to = ? WHERE file_idx = ?",
                        (offset, file_idx))
```

The load-bearing details:

- **The watermark and the rows commit atomically.** This is the crash-safety
  invariant: `indexed_to` always equals "every line before this offset is in
  `idx`, exactly once." Restart the indexer any time; it resumes from the
  watermark.
- **Partial lines are deferred, not indexed.** An app mid-`write()` can leave
  a torn last line; requiring the trailing `\n` means the indexer only ever
  records complete records. (This also relies on producers writing each log
  line in a single `write()` of a complete line — which is what O_APPEND
  line-oriented logging does — so concurrent appends from many apps don't
  interleave within a line.)
- **Rotation is detected by inode.** A recreated file (`logrotate`'s
  create mode) gets its watermark reset; a renamed-away file keeps its rows
  under the old `file_idx`. Lookups resolve `file_idx → path` through the
  `files` table, so renames need a path update, not a reindex. Compressing
  rotated files, however, invalidates their offsets — either index before
  compression and keep the plain file, or drop that file's rows at
  compression time and accept grep for cold archives.
- **One indexer, enforced.** If a second copy might ever start, take an
  exclusive advisory lock (e.g. `flock` on a pidfile) at startup. SQLite will
  serialize competing writers with `busy_timeout`, but two tailing indexers
  would double-insert rows — the lock is what prevents logical duplication.

### 2.3 Lookup against the live schema

```python
rows = con.execute(
    "SELECT f.path, i.offset FROM idx i JOIN files f USING (file_idx) "
    "WHERE i.uuid = ? ORDER BY f.file_idx, i.offset", (uuid,)).fetchall()
```

then seek/read/decrypt exactly as in Part 1. Readers should set
`PRAGMA busy_timeout = 5000` and open with `mode=ro`; under WAL they never
block on the indexer. One consistency note: the index trails the files by up
to one polling interval, so "not in the index yet" ≠ "not in the logs" — if
freshness matters for the tail end, follow the index lookup with a grep of
only the unindexed byte range (watermark → EOF), which is at most a few
seconds of recent log.

### 2.4 Retention

When a log file ages out: `DELETE FROM idx WHERE file_idx = ?`, delete the
`files` row, delete the file — in that order, one transaction for the SQL.
Run `PRAGMA incremental_vacuum` (or periodic `VACUUM`) if reclaiming index
disk matters. Because the index is derived, a botched retention pass is
always recoverable by rebuild.

### 2.5 Sizing expectations & when to graduate

Rules of thumb from the measurements here: the row-per-line schema costs
roughly 30–50 bytes per line indexed (rowid + key + B-tree overhead — a few
times larger than the packed-blob batch index), and a single indexer
comfortably sustains tens of thousands of inserts/sec in batched
transactions — far above typical log rates for a single host.

SQLite stops being the right tool when any of these become true: lookups must
span **multiple hosts' local files** (SQLite is a file, not a server); the
index itself needs high availability; or you want the store to *own* the log
data rather than point into files. That's the point to move the same
schema to Postgres (smallest step — the SQL above ports nearly verbatim), or
to a purpose-built log store (ClickHouse, Loki) if volume dominates. The
architecture — producers append, one ingester writes, readers query —
carries over unchanged; only the storage engine swaps.

---

*Numbers in this document come from the benchmark runs recorded in
`results/` and `report.md`; the batch implementation is `build_index.py`, the
lookup is the `index` strategy in `bench_strategies.py`. The Part 2 indexer
is implemented in `live_indexer.py` with a test suite
(`test_live_indexer.py`) covering incremental tailing, torn writes,
rename/truncate/delete rotation, crash-restart resume, the single-indexer
lock, read-only concurrent readers, and a 4-process concurrent-writer
simulation — verified on macOS and Linux. Its throughput has not been
benchmarked in this repo.*
