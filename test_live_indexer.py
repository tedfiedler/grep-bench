#!/usr/bin/env python3
"""Tests for live_indexer.py. Stdlib only:  python3 -m unittest test_live_indexer -v"""
import multiprocessing
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
import uuid as uuidlib

from live_indexer import LiveIndexer, lookup, open_db, remove_file


def uid(n):
    """Deterministic 36-char UUID for test n."""
    return str(uuidlib.UUID(int=n))


def make_line(u, i):
    return f'{{"ts":"2026-08-06T00:00:{i:02d}","id":"{u}","msg":"payload-{i}"}}\n'.encode()


def append(path, data):
    with open(path, "ab", buffering=0) as f:
        f.write(data)


def writer_proc(path, u, count):
    """Simulates one application appending complete lines (one write() each)."""
    for i in range(count):
        append(path, make_line(u, i))


class LiveIndexerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="liveidx-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.db = os.path.join(self.dir, "index.db")
        self.indexer = LiveIndexer(self.db, self.dir)
        self.addCleanup(self.indexer.close)
        self.con = self.indexer.con  # reuse for lookups in most tests

    def path(self, name):
        return os.path.join(self.dir, name)

    def idx_count(self):
        return self.con.execute("SELECT COUNT(*) FROM idx").fetchone()[0]

    # -- basics --------------------------------------------------------------

    def test_index_and_lookup(self):
        expected = {}
        for fi in range(3):
            p = self.path(f"app{fi}.jsonl")
            for i in range(20):
                u = uid(fi * 100 + i % 4)  # 4 uuids per file, interleaved
                line = make_line(u, i)
                append(p, line)
                expected.setdefault(u, []).append(line.rstrip(b"\n"))
        self.assertEqual(self.indexer.cycle(), 60)
        for u, lines in expected.items():
            self.assertEqual(lookup(self.con, u), lines)
        self.assertEqual(lookup(self.con, uid(999)), [])

    def test_incremental_append_no_duplicates(self):
        p = self.path("app.jsonl")
        append(p, make_line(uid(1), 0))
        self.assertEqual(self.indexer.cycle(), 1)
        append(p, make_line(uid(1), 1) + make_line(uid(2), 2))
        self.assertEqual(self.indexer.cycle(), 2)
        self.assertEqual(self.indexer.cycle(), 0)  # idempotent when quiet
        self.assertEqual(self.idx_count(), 3)
        self.assertEqual(len(lookup(self.con, uid(1))), 2)
        self.assertEqual(len(lookup(self.con, uid(2))), 1)

    def test_lines_without_id_advance_watermark(self):
        p = self.path("app.jsonl")
        append(p, b'{"ts":"t0","msg":"no id here"}\n' + make_line(uid(1), 0))
        self.assertEqual(self.indexer.cycle(), 1)  # only the id line indexed
        self.assertEqual(self.indexer.cycle(), 0)  # but both bytes consumed
        self.assertEqual(lookup(self.con, uid(1)), [make_line(uid(1), 0).rstrip(b"\n")])

    # -- torn writes ---------------------------------------------------------

    def test_partial_line_deferred_then_indexed_once(self):
        p = self.path("app.jsonl")
        whole, torn = make_line(uid(1), 0), make_line(uid(2), 1)
        append(p, whole + torn[:-10])  # last line missing its tail + newline
        self.assertEqual(self.indexer.cycle(), 1)
        self.assertEqual(lookup(self.con, uid(2)), [])
        append(p, torn[-10:])  # the write completes
        self.assertEqual(self.indexer.cycle(), 1)
        self.assertEqual(lookup(self.con, uid(2)), [torn.rstrip(b"\n")])
        self.assertEqual(self.idx_count(), 2)

    def test_tail_lookup_sees_unindexed_lines(self):
        p = self.path("app.jsonl")
        append(p, make_line(uid(1), 0))
        self.indexer.cycle()
        append(p, make_line(uid(1), 1))  # written, not yet cycled
        self.assertEqual(len(lookup(self.con, uid(1))), 1)
        self.assertEqual(len(lookup(self.con, uid(1), include_tail=True)), 2)
        self.indexer.cycle()
        # after the cycle the tail is empty again: no double counting
        self.assertEqual(len(lookup(self.con, uid(1), include_tail=True)), 2)

    # -- rotation / truncation / deletion ------------------------------------

    def test_rename_rotation_keeps_rows(self):
        p = self.path("app.jsonl")
        append(p, make_line(uid(1), 0))
        self.indexer.cycle()
        os.rename(p, self.path("app.1.jsonl"))  # logrotate-style rename
        append(p, make_line(uid(2), 1))         # fresh file at the old path
        self.assertEqual(self.indexer.cycle(), 1)  # only the new line
        self.assertEqual(self.idx_count(), 2)
        self.assertEqual(len(lookup(self.con, uid(1))), 1)  # via updated path
        self.assertEqual(len(lookup(self.con, uid(2))), 1)

    def test_truncate_purges_and_reindexes(self):
        p = self.path("app.jsonl")
        append(p, make_line(uid(1), 0) + make_line(uid(1), 1))
        self.indexer.cycle()
        with open(p, "wb"):  # copytruncate: same inode, size 0
            pass
        append(p, make_line(uid(2), 2))
        self.assertEqual(self.indexer.cycle(), 1)
        self.assertEqual(lookup(self.con, uid(1)), [])
        self.assertEqual(len(lookup(self.con, uid(2))), 1)
        self.assertEqual(self.idx_count(), 1)

    def test_deleted_file_purged(self):
        p = self.path("app.jsonl")
        append(p, make_line(uid(1), 0))
        self.indexer.cycle()
        os.remove(p)
        self.indexer.cycle()
        self.assertEqual(self.idx_count(), 0)
        self.assertEqual(lookup(self.con, uid(1)), [])

    def test_retention_helper(self):
        keep, drop = self.path("keep.jsonl"), self.path("drop.jsonl")
        append(keep, make_line(uid(1), 0))
        append(drop, make_line(uid(2), 0))
        self.indexer.cycle()
        remove_file(self.con, drop)
        self.assertEqual(lookup(self.con, uid(2)), [])
        self.assertEqual(len(lookup(self.con, uid(1))), 1)

    # -- crash safety --------------------------------------------------------

    def test_restart_resumes_from_watermark(self):
        p = self.path("app.jsonl")
        append(p, make_line(uid(1), 0))
        self.indexer.cycle()
        self.indexer.close()
        append(p, make_line(uid(1), 1))
        self.indexer = LiveIndexer(self.db, self.dir)  # "restart"
        self.addCleanup(self.indexer.close)
        self.con = self.indexer.con
        self.assertEqual(self.indexer.cycle(), 1)  # only the new line
        self.assertEqual(len(lookup(self.con, uid(1))), 2)
        self.assertEqual(self.idx_count(), 2)

    # -- concurrency ---------------------------------------------------------

    def test_single_indexer_lock(self):
        with self.assertRaises(RuntimeError):
            LiveIndexer(self.db, self.dir)

    def test_readonly_reader_while_indexer_open(self):
        append(self.path("app.jsonl"), make_line(uid(1), 0))
        self.indexer.cycle()
        rcon = open_db(self.db, read_only=True)
        self.addCleanup(rcon.close)
        self.assertEqual(len(lookup(rcon, uid(1))), 1)
        with self.assertRaises(sqlite3.OperationalError):
            rcon.execute("INSERT INTO idx VALUES ('x', 0, 0)")

    def test_many_concurrent_writers(self):
        """4 'applications' append to a shared file and one file each, while
        the indexer cycles concurrently. Every line must be indexed exactly
        once and be retrievable byte-identical."""
        shared = self.path("shared.jsonl")
        per_writer_lines = 200
        procs = []
        for w in range(4):
            for target in (shared, self.path(f"writer{w}.jsonl")):
                procs.append(
                    multiprocessing.Process(
                        target=writer_proc, args=(target, uid(1000 + w), per_writer_lines)
                    )
                )
        for pr in procs:
            pr.start()
        while any(pr.is_alive() for pr in procs):
            self.indexer.cycle()  # index while writes are in flight
            time.sleep(0.01)
        for pr in procs:
            pr.join()
            self.assertEqual(pr.exitcode, 0)
        self.indexer.cycle()  # sweep up the remainder

        total = 4 * 2 * per_writer_lines
        self.assertEqual(self.idx_count(), total)
        for w in range(4):
            lines = lookup(self.con, uid(1000 + w))
            self.assertEqual(len(lines), 2 * per_writer_lines)
            for line in lines:  # byte-identical, uncorrupted records
                self.assertTrue(line.startswith(b'{"ts":"'), line)
                self.assertIn(uid(1000 + w).encode(), line)


if __name__ == "__main__":
    unittest.main()
