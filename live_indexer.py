#!/usr/bin/env python3
"""Incremental UUID -> offset indexer for live JSONL logs (INDEXING.md, Part 2).

Many applications append complete JSON lines to log files; exactly ONE
LiveIndexer process tails those files and maintains a SQLite index mapping
each line's "id" UUID to (file, byte offset). Lookup readers open the
database read-only and, thanks to WAL, are never blocked by the indexer.

The index is derived data: delete the database and re-run cycles to rebuild
it from the files.

Design points (details in INDEXING.md):
- File identity is the inode, not the path: renames (logrotate) keep their
  rows and just update the recorded path. Note the standard caveat that a
  deleted file's inode may eventually be reused by the OS; a reused inode at
  a different path is indistinguishable from a rename, so pair this indexer
  with rotation schemes that rename or truncate rather than delete+recreate
  many files rapidly.
- A truncated file (copytruncate rotation) or a vanished inode invalidates
  its offsets, so that file's rows are purged (and re-indexed from offset 0
  if content reappears).
- Index rows and the per-file byte watermark commit in one transaction:
  after a crash, the indexer resumes exactly where the last commit left off,
  never double-indexing or skipping a line.
- Only complete (newline-terminated) lines are indexed; a torn final line is
  picked up on a later cycle once its newline arrives.
- An exclusive flock on <db>.lock enforces the single-indexer rule.
"""
import argparse
import fcntl
import glob
import json
import os
import sqlite3
import sys
import time

UUID_LEN = 36
_ID_KEY = b'"id":"'

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_idx    INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    inode       INTEGER NOT NULL,
    indexed_to  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS idx (
    uuid     TEXT    NOT NULL,
    file_idx INTEGER NOT NULL,
    offset   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uuid ON idx(uuid);
"""


def open_db(path, read_only=False):
    if read_only:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.executescript(SCHEMA)
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def extract_uuid(line):
    """Return the line's id UUID as str, or None if absent/malformed."""
    p = line.find(_ID_KEY)
    if p == -1:
        return None
    uid = line[p + len(_ID_KEY) : p + len(_ID_KEY) + UUID_LEN]
    if len(uid) != UUID_LEN:
        return None
    try:
        return uid.decode("ascii")
    except UnicodeDecodeError:
        return None


class LiveIndexer:
    def __init__(self, db_path, log_dir, pattern="*.jsonl", on_cycle=None):
        """on_cycle: optional callback invoked after every cycle with the
        metrics dict (see cycle()). Exceptions it raises are reported to
        stderr but never interrupt indexing."""
        self.log_dir = log_dir
        self.pattern = pattern
        self.on_cycle = on_cycle
        self.last_metrics = None
        self._cycles = 0
        self._lines_total = 0
        self._lock_file = open(db_path + ".lock", "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise RuntimeError(f"another indexer already holds {db_path}.lock")
        self.con = open_db(db_path)

    def close(self):
        self.con.close()
        self._lock_file.close()  # releases the flock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- one polling cycle ---------------------------------------------------

    def cycle(self):
        """Reconcile the file set, then tail every file. Returns lines indexed.

        Also updates self.last_metrics and fires the on_cycle hook with:
          ts              wall-clock time the cycle finished (epoch seconds)
          cycle           1-based cycle counter for this process
          duration_s      how long the cycle took
          lines_indexed   lines added this cycle
          lines_total     lines added since this process started
          files           number of tracked files
          lag_bytes       {path: unindexed bytes} per file (torn tail bytes,
                          or backlog the cycle didn't reach)
          lag_bytes_total sum of the above — 0 means fully caught up as of
                          the sizes observed at cycle start
        """
        t0 = time.perf_counter()
        stats = {}
        for path in glob.glob(os.path.join(self.log_dir, self.pattern)):
            try:
                stats[path] = os.stat(path)
            except OSError:
                continue  # vanished between glob and stat
        self._reconcile(stats)

        indexed = 0
        rows = self.con.execute(
            "SELECT file_idx, path, indexed_to FROM files ORDER BY file_idx"
        ).fetchall()
        for file_idx, path, watermark in rows:
            st = stats.get(path)
            if st is None or st.st_size <= watermark:
                continue
            indexed += self._tail(file_idx, path, watermark)

        lag = {}
        for path, watermark in self.con.execute("SELECT path, indexed_to FROM files"):
            st = stats.get(path)
            if st is not None:
                lag[path] = max(0, st.st_size - watermark)
        self._cycles += 1
        self._lines_total += indexed
        self.last_metrics = {
            "ts": round(time.time(), 3),
            "cycle": self._cycles,
            "duration_s": round(time.perf_counter() - t0, 4),
            "lines_indexed": indexed,
            "lines_total": self._lines_total,
            "files": len(lag),
            "lag_bytes": lag,
            "lag_bytes_total": sum(lag.values()),
        }
        if self.on_cycle is not None:
            try:
                self.on_cycle(self.last_metrics)
            except Exception as e:  # a metrics sink must never stop indexing
                print(f"[indexer] on_cycle hook failed: {e!r}", file=sys.stderr)
        return indexed

    def _reconcile(self, stats):
        """Match files rows to on-disk files by inode; handle rename/truncate/delete."""
        inode_to_path = {st.st_ino: p for p, st in stats.items()}
        with self.con:
            for file_idx, path, inode, watermark in self.con.execute(
                "SELECT file_idx, path, inode, indexed_to FROM files"
            ).fetchall():
                cur_path = inode_to_path.pop(inode, None)
                if cur_path is None:
                    # inode gone: file deleted or rotated out of scope —
                    # its offsets are unreadable, purge
                    self.con.execute("DELETE FROM idx WHERE file_idx = ?", (file_idx,))
                    self.con.execute("DELETE FROM files WHERE file_idx = ?", (file_idx,))
                    continue
                if cur_path != path:  # renamed (e.g. logrotate): rows stay valid
                    self.con.execute(
                        "UPDATE files SET path = ? WHERE file_idx = ?", (cur_path, file_idx)
                    )
                if stats[cur_path].st_size < watermark:
                    # truncated in place (copytruncate): old offsets invalid
                    self.con.execute("DELETE FROM idx WHERE file_idx = ?", (file_idx,))
                    self.con.execute(
                        "UPDATE files SET indexed_to = 0 WHERE file_idx = ?", (file_idx,)
                    )
            # anything left in inode_to_path is a genuinely new file
            for inode, path in sorted((i, p) for i, p in inode_to_path.items()):
                self.con.execute(
                    "INSERT INTO files (path, inode, indexed_to) VALUES (?, ?, 0)",
                    (path, inode),
                )

    def _tail(self, file_idx, path, watermark):
        batch = []
        offset = watermark
        with open(path, "rb") as f:
            f.seek(watermark)
            for line in f:
                if not line.endswith(b"\n"):
                    break  # torn final line: wait for a later cycle
                uid = extract_uuid(line)
                if uid is not None:
                    batch.append((uid, file_idx, offset))
                offset += len(line)
        if offset == watermark:
            return 0
        # rows + watermark move together: the crash-safety invariant
        with self.con:
            self.con.executemany("INSERT INTO idx VALUES (?, ?, ?)", batch)
            self.con.execute(
                "UPDATE files SET indexed_to = ? WHERE file_idx = ?", (offset, file_idx)
            )
        return len(batch)


# -- reader side -------------------------------------------------------------

def lookup(con, uuid, include_tail=False):
    """Return all indexed lines (bytes, newline-stripped) for a UUID.

    With include_tail=True, also scans each file's unindexed tail
    (watermark -> EOF, complete lines only) so results are current even
    between indexer cycles.
    """
    lines = []
    handles = {}

    def handle(path):
        if path not in handles:
            handles[path] = open(path, "rb")
        return handles[path]

    try:
        for path, offset in con.execute(
            "SELECT f.path, i.offset FROM idx i JOIN files f ON f.file_idx = i.file_idx "
            "WHERE i.uuid = ? ORDER BY i.file_idx, i.offset",
            (uuid,),
        ):
            f = handle(path)
            f.seek(offset)
            lines.append(f.readline().rstrip(b"\n"))
        if include_tail:
            for path, watermark in con.execute(
                "SELECT path, indexed_to FROM files ORDER BY file_idx"
            ):
                f = handle(path)
                f.seek(watermark)
                for line in f:
                    if not line.endswith(b"\n"):
                        break
                    if extract_uuid(line) == uuid:
                        lines.append(line.rstrip(b"\n"))
    finally:
        for f in handles.values():
            f.close()
    return lines


def remove_file(con, path):
    """Retention: drop a file's rows from the index (deleting the file itself
    is the caller's job; a later cycle would also purge it automatically)."""
    with con:
        for (file_idx,) in con.execute(
            "SELECT file_idx FROM files WHERE path = ?", (path,)
        ).fetchall():
            con.execute("DELETE FROM idx WHERE file_idx = ?", (file_idx,))
            con.execute("DELETE FROM files WHERE file_idx = ?", (file_idx,))


# -- CLI ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--db", default=None, help="default: <data-dir>/live-index.db")
    ap.add_argument("--pattern", default="*.jsonl")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between cycles (0 = run one cycle and exit)")
    ap.add_argument("--metrics", action="store_true",
                    help="emit one JSON metrics line per cycle on stdout")
    args = ap.parse_args()
    db = args.db or os.path.join(args.data_dir, "live-index.db")

    with LiveIndexer(db, args.data_dir, args.pattern) as indexer:
        while True:
            n = indexer.cycle()
            if args.metrics:
                print(json.dumps(indexer.last_metrics), flush=True)
            elif n:
                print(f"[indexer] +{n} lines", flush=True)
            if args.interval <= 0:
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
