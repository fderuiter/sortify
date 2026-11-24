"""Regression tests for consolidated MoveLedger and chunked SQLite commits."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from app.cli.ledger_cli import handle_ledger_command
from app.config import AppSettings
from app.core import db_conn, resilient_file_ops
from app.core import ledger as ledger_mod
from app.core import mover as mover_mod
from app.core.db import Database
from app.core.extractor import build_corpus_generator, build_corpus_generator_async
from app.core.history import HistoryManager, open_session_history
from app.core.ledger import KeyAuditLedger
from app.core.moves import INTENT, VERIFIED
from app.core.pipeline import SortPipeline, SortResult
from app.core.services import acquire_services
from app.core.session import AppSession
from app.core.session_catalog import find_abandoned_sessions, list_sessions
from app.core.store import Store, open_store

TOPICS = (
    ("invoice billing payment receipt tax ledger", "Finance"),
    ("contract legal agreement nda clause compliance", "Legal"),
    ("architecture roadmap engineering sprint deployment", "Engineering"),
    ("campaign marketing launch social analytics brand", "Marketing"),
)


def _populate_topic_files(folder: Path, count: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for idx in range(count):
        words, label = TOPICS[idx % len(TOPICS)]
        (folder / f"doc_{idx:03d}_{label.lower()}.txt").write_text(
            f"Document {idx:03d} ({label})\n{words} item_{idx}\n",
            encoding="utf-8",
        )


def _install_sqlite_commit_tracer(
    monkeypatch: pytest.MonkeyPatch, target_db_name: str = "sortify.db"
):
    """Wrap db_conn.sqlite3 so commits, write threads, and active transactions are tracked."""
    orig_sqlite3 = db_conn.sqlite3
    state: dict[str, Any] = {
        "phase": "setup",
        "commits_by_phase": {},
        "non_worker_writes": [],
        "tracked_conns": [],
    }
    lock = threading.Lock()

    class _ConnProxy:
        def __init__(self, inner: Any) -> None:
            object.__setattr__(self, "_inner", inner)

        def __getattr__(self, name: str) -> Any:
            return getattr(object.__getattribute__(self, "_inner"), name)

        def __setattr__(self, name: str, value: Any) -> None:
            setattr(object.__getattribute__(self, "_inner"), name, value)

        def __enter__(self) -> Any:
            return object.__getattribute__(self, "_inner").__enter__()

        def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
            inner = object.__getattribute__(self, "_inner")
            was_in_tx = bool(inner.in_transaction) and exc_type is None
            out = inner.__exit__(exc_type, exc_val, exc_tb)
            if was_in_tx:
                with lock:
                    ph = state["phase"]
                    state["commits_by_phase"][ph] = (
                        state["commits_by_phase"].get(ph, 0) + 1
                    )
            return out

        def commit(self) -> None:
            inner = object.__getattribute__(self, "_inner")
            was_in_tx = bool(inner.in_transaction)
            inner.commit()
            if was_in_tx:
                with lock:
                    ph = state["phase"]
                    state["commits_by_phase"][ph] = (
                        state["commits_by_phase"].get(ph, 0) + 1
                    )

    class _SqliteProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(orig_sqlite3, name)

        def connect(self, database: str, *args: Any, **kwargs: Any) -> Any:
            raw = orig_sqlite3.connect(database, *args, **kwargs)
            if os.path.basename(str(database)) == target_db_name:

                def _trace(sql: str) -> None:
                    stmt = sql.strip().upper()
                    if stmt.startswith(
                        (
                            "INSERT",
                            "UPDATE",
                            "DELETE",
                            "CREATE",
                            "ALTER",
                            "DROP",
                            "REPLACE",
                        )
                    ):
                        with lock:
                            ph = state["phase"]
                        if ph != "setup":
                            tname = threading.current_thread().name
                            if not tname.startswith("DBWorkerThread"):
                                with lock:
                                    state["non_worker_writes"].append(
                                        (ph, tname, stmt[:80])
                                    )

                raw.set_trace_callback(_trace)
                with lock:
                    state["tracked_conns"].append(raw)
                return _ConnProxy(raw)
            return raw

    monkeypatch.setattr(db_conn, "sqlite3", _SqliteProxy())
    return state, lock


def test_200_file_pipeline_sort_and_undo_commit_count_and_single_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200-file sort + undo executes <= 25 SQLite commits, zero non-DBWorker writes, and zero transient ledger rows."""
    app_dir = tmp_path / "app_data"
    work_dir = tmp_path / "workspace"
    app_dir.mkdir()
    _populate_topic_files(work_dir, 200)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    state, _ = _install_sqlite_commit_tracer(monkeypatch)
    store = open_store(app_dir, legacy_session_root=tmp_path / "none")

    moves_inside_db_tx: list[str] = []
    orig_resilient_move = resilient_file_ops.resilient_move

    def checked_resilient_move(src: str, dst: str, *args: Any, **kwargs: Any) -> Any:
        for c in list(state["tracked_conns"]):
            try:
                if c.in_transaction:
                    moves_inside_db_tx.append(src)
            except Exception:
                pass
        return orig_resilient_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(resilient_file_ops, "resilient_move", checked_resilient_move)

    log_step_calls: list[Any] = []
    per_file_undo_db_calls: list[Any] = []

    if hasattr(HistoryManager, "log_step"):
        monkeypatch.setattr(
            HistoryManager,
            "log_step",
            lambda self, *a, **kw: log_step_calls.append((a, kw)),
        )
    assert not hasattr(ledger_mod, "TransactionLedger")

    orig_update_doc_path = Database.update_document_path

    def spy_update_doc_path(self: Database, *a: Any, **kw: Any) -> Any:
        if state["phase"] == "undo":
            per_file_undo_db_calls.append((a, kw))
        return orig_update_doc_path(self, *a, **kw)

    monkeypatch.setattr(Database, "update_document_path", spy_update_doc_path)

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    settings.MAX_FOLDERS = 4
    settings.MAX_WORKERS = 4

    state["phase"] = "session_init"
    session = AppSession(settings, str(work_dir), store=store)
    try:
        pipeline = SortPipeline(session)

        state["phase"] = "plan"
        plan = pipeline.plan(chunk_size=50)

        state["phase"] = "execute"
        sort_res = pipeline.execute(plan, chunk_size=50, batch_size=50)
        assert sort_res.ok
        assert len(sort_res.moved) == 200
        assert sort_res.session_id is not None

        state["phase"] = "undo"
        undo_res = pipeline.undo(sort_res.session_id)
        assert undo_res.ok
        assert len(undo_res.moved) == 200

        restored = [p.name for p in work_dir.iterdir() if p.is_file()]
        assert len(restored) == 200

        sconn = db_conn.get_db_connection(str(store.path))
        assert sconn.execute("SELECT COUNT(*) FROM step_ledger").fetchone()[0] == 0
        sidecar_path = app_dir / "transaction_ledger.db"
        if sidecar_path.exists():
            sc = db_conn.get_db_connection(str(sidecar_path))
            assert (
                sc.execute("SELECT COUNT(*) FROM transaction_ledger").fetchone()[0] == 0
            )
        assert (
            sconn.execute(
                "SELECT COUNT(*) FROM moves WHERE session_id = ? AND state = ?",
                (sort_res.session_id, VERIFIED),
            ).fetchone()[0]
            == 200
        )
    finally:
        state["phase"] = "setup"
        session.close()
        db_conn.clear_connection_cache(only_current_and_inactive=False)

    commits = state["commits_by_phase"]
    total_pipeline_commits = (
        commits.get("plan", 0) + commits.get("execute", 0) + commits.get("undo", 0)
    )
    assert total_pipeline_commits <= 25, f"Expected <= 25 commits, got {commits}"
    assert commits.get("plan", 0) <= 4
    assert commits.get("execute", 0) <= 10
    assert commits.get("undo", 0) <= 6
    assert state["non_worker_writes"] == []
    assert moves_inside_db_tx == []
    assert log_step_calls == []
    assert per_file_undo_db_calls == []


def test_extractor_and_analyzer_chunked_upserts_and_async_cancel_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction + partial_fit commits at most 1 upsert_documents per 50-file chunk and flushes on async cancel."""
    app_dir = tmp_path / "app_data"
    work_dir = tmp_path / "docs"
    app_dir.mkdir()
    _populate_topic_files(work_dir, 120)

    store = open_store(app_dir, legacy_session_root=tmp_path / "none")
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session = AppSession(settings, str(work_dir), store=store)
    try:
        upsert_batches: list[int] = []
        single_upserts: list[str] = []
        orig_upsert_docs = session.db.upsert_documents
        orig_upsert_doc = session.db.upsert_document

        def spy_upsert_docs(docs_data: list[Any]) -> None:
            upsert_batches.append(len(docs_data))
            orig_upsert_docs(docs_data)

        def spy_upsert_doc(base_dir: str, filepath: str, *a: Any, **kw: Any) -> None:
            single_upserts.append(filepath)
            orig_upsert_doc(base_dir, filepath, *a, **kw)

        monkeypatch.setattr(session.db, "upsert_documents", spy_upsert_docs)
        monkeypatch.setattr(session.db, "upsert_document", spy_upsert_doc)

        item_names = sorted(p.name for p in work_dir.iterdir() if p.is_file())

        def _noop_progress(*_args: Any, **_kwargs: Any) -> None:
            return None

        # First pass over 120 files (chunks of 50, 50, 20) -> 3 batch commits, 0 single-row commits
        for chunk in build_corpus_generator(
            str(work_dir),
            item_names,
            _noop_progress,
            db=session.db,
            chunk_size=50,
            max_workers=4,
            settings=settings,
        ):
            session.analyzer.partial_fit(
                str(work_dir), chunk, runtime_settings=settings
            )

        assert upsert_batches == [50, 50, 20]
        assert single_upserts == []

        # Second pass on unchanged files -> cached hashes hit per chunk, 0 DB writes
        upsert_batches.clear()
        second_chunks = list(
            build_corpus_generator(
                str(work_dir),
                item_names,
                _noop_progress,
                db=session.db,
                chunk_size=50,
                max_workers=4,
                settings=settings,
            )
        )
        assert sum(len(c) for c in second_chunks) == 120
        assert upsert_batches == []

        # Async generator cancelled mid-chunk -> flushes pending buffered rows before exiting
        async_dir = tmp_path / "async_docs"
        _populate_topic_files(async_dir, 30)
        async_items = sorted(p.name for p in async_dir.iterdir() if p.is_file())

        async def _consume_partial_and_close() -> int:
            agen = build_corpus_generator_async(
                str(async_dir),
                async_items,
                session.db,
                settings=settings,
                batch_size=50,
            )
            seen = 0
            try:
                async for _ in agen:
                    seen += 1
                    if seen >= 8:
                        break
            finally:
                await agen.aclose()
            return seen

        consumed = asyncio.run(_consume_partial_and_close())
        assert consumed == 8
        persisted_hashes = session.db.get_document_hashes(str(async_dir), async_items)
        assert len(persisted_hashes) >= 8
    finally:
        session.close()
        db_conn.clear_connection_cache(only_current_and_inactive=False)


def test_mover_pre_move_intent_batch_overwrite_displacement_and_intra_chunk_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mover commits pre-move intents before byte 1 moves, commits overwrite displacement synchronously, and handles intra-chunk collisions."""
    app_dir = tmp_path / "app_data"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    store = open_store(app_dir, legacy_session_root=tmp_path / "none")

    # Part A: Pre-move intent visibility before physical move #1 + intra-chunk duplicate leaf reservation
    work_a = tmp_path / "work_a"
    sub1 = work_a / "sub1"
    sub2 = work_a / "sub2"
    sub1.mkdir(parents=True)
    sub2.mkdir(parents=True)
    (sub1 / "report.txt").write_text("alpha duplicate leaf", encoding="utf-8")
    (sub2 / "report.txt").write_text("beta duplicate leaf", encoding="utf-8")
    for idx in range(10):
        (work_a / f"item_{idx}.txt").write_text(f"payload {idx}", encoding="utf-8")

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    settings.CONFLICT_POLICY = "rename"

    observed_intents_on_first_move: list[tuple[str, str | None, int | None]] = []
    first_move_lock = threading.Lock()
    orig_move = resilient_file_ops.resilient_move

    def check_intents_before_first_move(src: str, dst: str, *a: Any, **kw: Any) -> Any:
        with first_move_lock:
            if not observed_intents_on_first_move:
                conn = db_conn.get_db_connection(str(store.path))
                rows = conn.execute(
                    "SELECT state, src_sha256, size FROM moves ORDER BY seq ASC"
                ).fetchall()
                observed_intents_on_first_move.extend(rows)
        return orig_move(src, dst, *a, **kw)

    monkeypatch.setattr(
        resilient_file_ops, "resilient_move", check_intents_before_first_move
    )

    session_a = AppSession(settings, str(work_a), store=store)
    try:
        finance_nodes: dict[str, Any] = {
            "dup_1": {
                "__type__": "file",
                "relative_source": "../sub1/report.txt",
                "target_filename": "report.txt",
            },
            "dup_2": {
                "__type__": "file",
                "relative_source": "../sub2/report.txt",
                "target_filename": "report.txt",
            },
        }
        for idx in range(10):
            finance_nodes[f"item_{idx}.txt"] = {
                "__type__": "file",
                "relative_source": f"../item_{idx}.txt",
                "target_filename": f"item_{idx}.txt",
            }
        summary_a = mover_mod.execute_moves(
            str(work_a),
            {"Finance": finance_nodes},
            session_a.db,
            session_a.history_manager,
            runtime_settings=settings,
            chunk_size=50,
            batch_size=50,
        )
        res_a = SortResult.from_summary(summary_a, "_Quarantine")
        assert res_a.ok
        assert len(res_a.moved) == 12
        # All 12 intents were already committed on disk before byte 1 moved
        assert len(observed_intents_on_first_move) == 12
        assert all(
            status == INTENT and sha and len(sha) == 64 and (sz or 0) > 0
            for status, sha, sz in observed_intents_on_first_move
        )
        # Intra-chunk collision resolved without clobbering either report.txt
        finance_dir = work_a / "Finance"
        contents = {
            p.read_text(encoding="utf-8")
            for p in finance_dir.glob("report*.txt")
            if p.is_file()
        }
        assert contents == {"alpha duplicate leaf", "beta duplicate leaf"}
    finally:
        session_a.close()

    # Part B: Overwrite conflict policy commits displaced_to synchronously BEFORE overwriting target
    monkeypatch.setattr(resilient_file_ops, "resilient_move", orig_move)
    work_b = tmp_path / "work_b"
    (work_b / "Target").mkdir(parents=True)
    existing_target = work_b / "Target" / "clash.txt"
    incoming_src = work_b / "clash.txt"
    existing_target.write_text("existing user destination content", encoding="utf-8")
    incoming_src.write_text("incoming sorted content", encoding="utf-8")

    displaced_seen_before_move: list[str | None] = []

    def check_displacement_before_overwrite(
        src: str, dst: str, *a: Any, **kw: Any
    ) -> Any:
        if Path(src).resolve() == incoming_src.resolve():
            conn = db_conn.get_db_connection(str(store.path))
            row = conn.execute(
                "SELECT displaced FROM moves WHERE src = ? ORDER BY seq DESC LIMIT 1",
                (str(incoming_src),),
            ).fetchone()
            displaced_seen_before_move.append(row[0] if row else None)
        return orig_move(src, dst, *a, **kw)

    monkeypatch.setattr(
        resilient_file_ops, "resilient_move", check_displacement_before_overwrite
    )

    settings_ow = AppSettings()
    settings_ow.AI_CONSENT_GRANTED = False
    settings_ow.CONFLICT_POLICY = "overwrite"

    session_b = AppSession(settings_ow, str(work_b), store=store)
    try:
        plan_b = {
            "Target": {
                "clash.txt": {
                    "__type__": "file",
                    "relative_source": "../clash.txt",
                    "target_filename": "clash.txt",
                }
            }
        }
        pipe_b = SortPipeline(session_b)
        summary_b = mover_mod.execute_moves(
            str(work_b),
            plan_b,
            session_b.db,
            session_b.history_manager,
            runtime_settings=settings_ow,
            chunk_size=50,
            batch_size=50,
        )
        res_b = SortResult.from_summary(summary_b, "_Quarantine")
        assert res_b.ok
        assert len(displaced_seen_before_move) == 1
        assert displaced_seen_before_move[0] is not None
        assert Path(displaced_seen_before_move[0]).exists()

        # Undo restores both the moved file and the displaced existing target
        assert res_b.session_id is not None
        undo_b = pipe_b.undo(res_b.session_id)
        assert undo_b.ok
        assert incoming_src.read_text(encoding="utf-8") == "incoming sorted content"
        assert (
            existing_target.read_text(encoding="utf-8")
            == "existing user destination content"
        )
    finally:
        session_b.close()
        db_conn.clear_connection_cache(only_current_and_inactive=False)


def test_legacy_ledger_readonly_fallback_across_catalog_cli_and_audit_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session catalog, ledger CLI, MoveLedger, and audit export fall back read-only to legacy tables while keeping KeyAuditLedger intact."""
    app_dir = tmp_path / "app_data"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    store = open_store(app_dir, legacy_session_root=tmp_path / "none")

    services = acquire_services(store)
    try:
        # Seed a legacy completed session backed only by step_ledger rows (zero rows in moves)
        # and a legacy interrupted session backed only by step_ledger rows, plus an orphan moves intent.
        def _seed_legacy() -> None:
            conn = db_conn.get_db_connection(str(store.path))
            with conn:
                conn.execute(
                    """
                    INSERT INTO sessions (session_id, timestamp, base_dir, status)
                    VALUES
                        ('legacy_done', 1700000000.0, ?, 'completed'),
                        ('legacy_crashed', 1700000100.0, ?, 'active'),
                        ('ignored_intent_sess', 1700000150.0, ?, 'abandoned')
                    """,
                    (
                        str(tmp_path / "dir_done"),
                        str(tmp_path / "dir_crash"),
                        str(tmp_path / "dir_ignored"),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO step_ledger (session_id, source_path, target_path, original_path, original_filename, status, timestamp)
                    VALUES
                        ('legacy_done', '/tmp/a.txt', '/tmp/Docs/a.txt', '/tmp/a.txt', 'a.txt', 'completed', 1700000001.0),
                        ('legacy_done', '/tmp/b.txt', '/tmp/Docs/b.txt', '/tmp/b.txt', 'b.txt', 'completed', 1700000002.0),
                        ('legacy_crashed', '/tmp/c.txt', '/tmp/Docs/c.txt', '/tmp/c.txt', 'c.txt', 'completed', 1700000101.0)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO moves (session_id, seq, src, dst, state, created_at, updated_at)
                    VALUES
                        ('orphan_intent_sess', 1, '/tmp/o.txt', '/tmp/Docs/o.txt', 'intent', 1700000200.0, 1700000200.0),
                        ('ignored_intent_sess', 1, '/tmp/i.txt', '/tmp/Docs/i.txt', 'intent', 1700000151.0, 1700000151.0)
                    """
                )

        services.worker.execute_write(_seed_legacy)

        # 1a. Un-migrated legacy history DB (sessions + step_ledger, no moves table) reports legacy counts via list_sessions
        legacy_hist_db = tmp_path / "unmigrated_history.db"
        lh_conn = db_conn.get_db_connection(str(legacy_hist_db))
        with lh_conn:
            lh_conn.execute(
                "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            lh_conn.execute(
                "CREATE TABLE step_ledger (step_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, source_path TEXT, target_path TEXT, original_path TEXT)"
            )
            lh_conn.execute(
                "INSERT INTO sessions VALUES ('unmig_done', 1700000000.0, '/legacy', 'completed')"
            )
            lh_conn.execute(
                "INSERT INTO step_ledger (session_id, source_path, target_path, original_path) VALUES ('unmig_done', '/legacy/a.txt', '/legacy/Docs/a.txt', '/legacy/a.txt'), ('unmig_done', '/legacy/b.txt', '/legacy/Docs/b.txt', '/legacy/b.txt')"
            )
        legacy_catalog = list_sessions(Store(app_dir=tmp_path, path=legacy_hist_db))
        assert legacy_catalog[0]["session_id"] == "unmig_done"
        assert legacy_catalog[0]["step_count"] == 2
        assert legacy_catalog[0]["file_count"] == 2

        # 1b. Session catalog detects both legacy crashed session (backed by step_ledger) + orphan moves intent
        abandoned = {s["session_id"]: s for s in find_abandoned_sessions(store)}
        assert "legacy_crashed" in abandoned
        assert abandoned["legacy_crashed"]["step_count"] == 1
        assert "orphan_intent_sess" in abandoned
        assert abandoned["orphan_intent_sess"]["step_count"] == 1
        assert "ignored_intent_sess" not in abandoned

        # 2. Audit export reads legacy step_ledger rows without mutating them
        export_path = tmp_path / "legacy_audit.json"
        with open_session_history(store) as hm:
            hm.export_audit_log(
                str(export_path), session_id="legacy_done", format="json"
            )
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert len(exported["steps"]) == 2
        assert {r["original_path"] for r in exported["steps"]} == {
            "/tmp/a.txt",
            "/tmp/b.txt",
        }

        conn = db_conn.get_db_connection(str(store.path))
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM step_ledger WHERE session_id = 'legacy_done'"
            ).fetchone()[0]
            == 2
        )

        # 3. Custom --ledger-db containing only legacy transaction_ledger table works read-only in CLI
        legacy_sidecar = tmp_path / "legacy_tx.db"
        KeyAuditLedger(db_path=str(legacy_sidecar))
        sidecar_conn = db_conn.get_db_connection(str(legacy_sidecar))
        with sidecar_conn:
            sidecar_conn.execute(
                """
                INSERT INTO transaction_ledger (
                    entry_id, session_id, base_dir, source_path, dest_path,
                    source_rel_path, dest_rel_path, status, created_at, updated_at
                ) VALUES (
                    'e1', 'legacy_cli_sess', '/tmp', '/tmp/x.txt', '/tmp/Docs/x.txt',
                    'x.txt', 'Docs/x.txt', 'INTENT', 1700000300.0, 1700000300.0
                )
                """
            )

        args = argparse.Namespace(
            subcommand="ledger",
            ledger_command="status",
            ledger_db=str(legacy_sidecar),
            session_id=None,
            failed=False,
            aborted=False,
            json=True,
        )
        with pytest.raises(SystemExit) as cli_exc:
            handle_ledger_command(args, AppSettings())
        assert cli_exc.value.code == 0
        cli_payload = json.loads(capsys.readouterr().out)
        assert cli_payload["status"] == "success"
        assert cli_payload["count"] == 1
        assert cli_payload["pending_entries"][0]["session_id"] == "legacy_cli_sess"
        assert (
            sidecar_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='moves'"
            ).fetchone()
            is None
        )

        # 4. KeyAuditLedger remains intact for cryptographic audit events
        key_audit = KeyAuditLedger(db_path=str(app_dir / "transaction_ledger.db"))
        event_id = key_audit.log_key_audit(
            event_type="KEY_GENERATED",
            db_path_hash="test-db-hash",
            status="SUCCESS",
        )
        assert event_id.startswith("audit:")
        events = key_audit.get_key_audit_events(db_path_hash="test-db-hash", limit=5)
        assert any(e["event_id"] == event_id for e in events)
    finally:
        services.close()
        db_conn.clear_connection_cache(only_current_and_inactive=False)


def test_mover_duck_typed_single_item_ledger_fallback() -> None:
    """Chunked mover helpers gracefully fall back when a duck-typed ledger lacks batch methods."""

    class _MinimalMovesLedger:
        def __init__(self) -> None:
            self.events: list[tuple[str, Any]] = []
            self._seq = 0

        def record_intent(
            self,
            session_id: str,
            src: str,
            dst: str,
            *,
            src_sha256: str | None = None,
            size: int | None = None,
        ) -> int:
            self._seq += 1
            self.events.append(("intent", (session_id, src, dst, src_sha256, size)))
            return self._seq

        def mark_done(
            self,
            session_id: str,
            seq: int,
            *,
            dst: str | None = None,
            displaced: str | None = None,
        ) -> None:
            self.events.append(("done", (session_id, seq, dst, displaced)))

        def mark_verified(self, session_id: str, seq: int) -> None:
            self.events.append(("verified", (session_id, seq)))

        def mark_failed(
            self, session_id: str, seq: int, error: str | None = None
        ) -> None:
            self.events.append(("failed", (session_id, seq, error)))

    ledger = _MinimalMovesLedger()
    assert mover_mod._record_move_intents_batch(ledger, "s1", []) == []
    assert mover_mod._record_move_intents_batch(
        None, "s1", [("/a", "/b", "sha", 10)]
    ) == [None]
    seqs = mover_mod._record_move_intents_batch(
        ledger,
        "s1",
        [("/a", "/b", "sha1", 10), ("/c", "/d", None, None), ("/e", "/f", "sha3", 5)],
    )
    assert seqs == [1, 2, 3]
    assert (
        mover_mod._flush_chunk_completions_and_db(None, ledger, "s1", [], []) is False
    )
    flushed = mover_mod._flush_chunk_completions_and_db(
        None,
        ledger,
        "s1",
        [
            {"seq": 1, "state": VERIFIED, "dst": "/b", "displaced": None},
            {"seq": 2, "state": "done", "dst": "/d", "displaced": "/bak"},
            {"seq": 3, "state": "failed", "error": "boom"},
        ],
        [],
    )
    assert flushed is True
    kinds = [kind for kind, _ in ledger.events]
    assert kinds == [
        "intent",
        "intent",
        "intent",
        "done",
        "verified",
        "done",
        "failed",
    ]


def test_200_file_pipeline_with_duplicate_staging_uses_chunked_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200-file sort + undo with DUPLICATE_ACTION='stage' batches disposal moves into <= 25 SQLite commits."""
    app_dir = tmp_path / "app_data_dup"
    work_dir = tmp_path / "workspace_dup"
    app_dir.mkdir()
    work_dir.mkdir()
    for idx in range(100):
        words, label = TOPICS[idx % len(TOPICS)]
        body = f"Document {idx:03d} ({label})\n{words} cluster_{idx}\n"
        (work_dir / f"orig_{idx:03d}_{label.lower()}.txt").write_text(
            body, encoding="utf-8"
        )
        (work_dir / f"z_copy_{idx:03d}_{label.lower()}.txt").write_text(
            body, encoding="utf-8"
        )
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    state, _ = _install_sqlite_commit_tracer(monkeypatch)
    store = open_store(app_dir, legacy_session_root=tmp_path / "none")

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    settings.MAX_FOLDERS = 4
    settings.MAX_WORKERS = 4
    settings.DUPLICATE_ACTION = "stage"
    settings.DETECT_ORIGINAL_BY = "name"

    state["phase"] = "session_init"
    session = AppSession(settings, str(work_dir), store=store)
    try:
        pipeline = SortPipeline(session)

        state["phase"] = "plan"
        plan = pipeline.plan(chunk_size=50)

        state["phase"] = "execute"
        sort_res = pipeline.execute(plan, chunk_size=50, batch_size=50)
        assert sort_res.ok
        assert sort_res.session_id is not None
        assert len(sort_res.summary.get("staged_duplicates", [])) == 100

        state["phase"] = "undo"
        undo_res = pipeline.undo(sort_res.session_id)
        assert undo_res.ok
        assert len(undo_res.moved) == 200

        restored = [p.name for p in work_dir.iterdir() if p.is_file()]
        assert len(restored) == 200

        commits = state["commits_by_phase"]
        total_commits = (
            commits.get("plan", 0) + commits.get("execute", 0) + commits.get("undo", 0)
        )
        assert total_commits <= 25, f"Expected <= 25 commits, got {commits}"
        assert state["non_worker_writes"] == []
    finally:
        state["phase"] = "setup"
        session.close()
        db_conn.clear_connection_cache(only_current_and_inactive=False)
