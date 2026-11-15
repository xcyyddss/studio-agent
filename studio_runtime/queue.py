"""SQLite execution ledger. No network retry is implicit in recovery."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .contracts import JobSpec, canonical, digest


class QueueConflict(ValueError):
    pass


class QueueStore:
    def __init__(self, db_path, *, read_only=False):
        self.db_path = Path(db_path).resolve()
        self.read_only = read_only
        if read_only:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, spec TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL, current_attempt TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    state TEXT NOT NULL, owner TEXT NOT NULL, lease_until REAL NOT NULL,
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
                    worker_pid INTEGER, artifact_dir TEXT, predecessor TEXT, note TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id));
                CREATE TABLE IF NOT EXISTS results (
                    result_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL, acked_at REAL);
                CREATE TABLE IF NOT EXISTS controls (run_id TEXT PRIMARY KEY, paused INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
                    event TEXT NOT NULL, job_id TEXT, attempt_id TEXT, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id,state);
                CREATE INDEX IF NOT EXISTS attempts_job ON attempts(job_id,sequence);
            """)

    def _connect(self):
        connection = sqlite3.connect(self.db_path.as_uri()+'?mode=ro', uri=True, timeout=30) if self.read_only else sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _event(connection, event, job_id=None, attempt_id=None, **data):
        connection.execute("INSERT INTO execution_events(created_at,event,job_id,attempt_id,data) VALUES(?,?,?,?,?)",
                           (time.time(), event, job_id, attempt_id, canonical(data)))

    def enqueue(self, job: JobSpec) -> str:
        if isinstance(job, dict):
            job = JobSpec.from_dict(job)
        spec, checksum = canonical(job.to_dict()), digest(job.to_dict())
        conflict = False
        with self._transaction() as connection:
            previous = connection.execute("SELECT spec_sha256 FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
            if previous:
                if previous[0] != checksum:
                    self._event(connection, "enqueue_conflict", job.job_id, existing_sha256=previous[0], offered_sha256=checksum, offered_spec=job.to_dict())
                    conflict = True
            else:
                now = time.time()
                connection.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                    (job.job_id, job.run_id, spec, checksum, "queued", None, now, now))
                self._event(connection, "enqueued", job.job_id)
        if conflict:
            raise QueueConflict("same job_id has different immutable input")
        return job.job_id

    def get_job(self, job_id) -> JobSpec:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT spec FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return JobSpec.from_dict(json.loads(row[0]))

    def pause(self, run_id):
        self._set_paused(run_id, True)

    def resume(self, run_id):
        self._set_paused(run_id, False)

    def _set_paused(self, run_id, paused):
        with self._transaction() as connection:
            connection.execute("INSERT INTO controls VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET paused=excluded.paused", (run_id, int(paused)))
            self._event(connection, "paused" if paused else "resumed", run_id=run_id)

    def claim(self, owner, lease_seconds=30, run_id=None, max_active=6):
        if lease_seconds <= 0 or max_active < 1:
            raise ValueError("positive lease and worker limit required")
        with self._transaction() as connection:
            active = connection.execute("SELECT count(*) FROM attempts WHERE state IN ('leased','running')").fetchone()[0]
            if active >= max_active:
                return None
            sql = "SELECT j.* FROM jobs j LEFT JOIN controls c ON c.run_id=j.run_id WHERE j.state='queued' AND COALESCE(c.paused,0)=0"
            args = []
            if run_id is not None:
                sql += " AND j.run_id=?"
                args.append(run_id)
            row = connection.execute(sql + " ORDER BY j.created_at,j.job_id LIMIT 1", args).fetchone()
            if row is None:
                return None
            attempt_id, now = "attempt-" + uuid.uuid4().hex, time.time()
            sequence = connection.execute("SELECT count(*) FROM attempts WHERE job_id=?", (row["job_id"],)).fetchone()[0] + 1
            connection.execute("INSERT INTO attempts(attempt_id,job_id,sequence,state,owner,lease_until,created_at,predecessor) VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, row["job_id"], sequence, "leased", owner, now + lease_seconds, now, row["current_attempt"]))
            connection.execute("UPDATE jobs SET state='leased',current_attempt=?,updated_at=? WHERE job_id=?", (attempt_id, now, row["job_id"]))
            self._event(connection, "leased", row["job_id"], attempt_id, owner=owner)
            return {"job": json.loads(row["spec"]), "attempt_id": attempt_id, "sequence": sequence}

    def set_artifact_dir(self, attempt_id, artifact_dir):
        with self._transaction() as connection:
            connection.execute("UPDATE attempts SET artifact_dir=? WHERE attempt_id=? AND state='leased'", (str(Path(artifact_dir).resolve()), attempt_id))

    def begin_attempt(self, attempt_id, worker_pid, lease_seconds=30):
        with self._transaction() as connection:
            row = connection.execute("SELECT a.*,j.current_attempt FROM attempts a JOIN jobs j USING(job_id) WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["state"] != "leased" or row["current_attempt"] != attempt_id or row["lease_until"] < time.time():
                return False
            now = time.time()
            connection.execute("UPDATE attempts SET state='running',worker_pid=?,started_at=?,lease_until=? WHERE attempt_id=?", (worker_pid, now, now + lease_seconds, attempt_id))
            connection.execute("UPDATE jobs SET state='running',updated_at=? WHERE job_id=?", (now, row["job_id"]))
            self._event(connection, "running", row["job_id"], attempt_id, worker_pid=worker_pid)
            return True

    def heartbeat(self, attempt_id, lease_seconds=30):
        with self._transaction() as connection:
            changed = connection.execute("UPDATE attempts SET lease_until=? WHERE attempt_id=? AND state='running'", (time.time() + lease_seconds, attempt_id)).rowcount
        return bool(changed)

    def recover_expired(self, now=None):
        now = time.time() if now is None else now
        recovered = []
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM attempts WHERE state IN ('leased','running') AND lease_until<?", (now,)).fetchall()
            for row in rows:
                # A leased worker must atomically begin before executing anything. Expired
                # leased attempts cannot begin, so they alone are safe to requeue.
                state = "not_started" if row["state"] == "leased" else "uncertain"
                connection.execute("UPDATE attempts SET state=?,note=? WHERE attempt_id=?", (state, "lease expired", row["attempt_id"]))
                connection.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=? AND current_attempt=?", ("queued" if state == "not_started" else "uncertain", now, row["job_id"], row["attempt_id"]))
                self._event(connection, "lease_expired", row["job_id"], row["attempt_id"], resulting_state=state)
                recovered.append({"attempt_id": row["attempt_id"], "state": state})
        return recovered

    def complete(self, attempt_id, result):
        if result.get("attempt_id") != attempt_id:
            raise QueueConflict("attempt/result mismatch")
        if result.get("outcome") not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("invalid result outcome")
        expected = digest({key: value for key, value in result.items() if key != "result_sha256"})
        checksum = result.get("result_sha256") or expected
        if checksum != expected:
            raise QueueConflict("result content does not match its digest")
        with self._transaction() as connection:
            existing = connection.execute("SELECT result_id,result_sha256 FROM results WHERE attempt_id=?", (attempt_id,)).fetchone()
            if existing:
                if existing["result_sha256"] != checksum:
                    raise QueueConflict("conflicting result for an immutable attempt")
                return existing["result_id"]
            row = connection.execute("SELECT a.*,j.run_id,j.spec,j.current_attempt FROM attempts a JOIN jobs j USING(job_id) WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["job_id"] != result.get("job_id"):
                raise QueueConflict("unknown attempt or wrong job")
            if row["state"] not in {"running", "uncertain"}:
                raise QueueConflict("a non-started attempt cannot produce a result")
            spec = json.loads(row["spec"])
            supplied_job_hash = result.get("receipt", {}).get("job_sha256")
            if supplied_job_hash and supplied_job_hash != digest(spec):
                raise QueueConflict("receipt input digest differs from queued job")
            result_id = "result-" + attempt_id.removeprefix("attempt-")
            body = dict(result, result_id=result_id, result_sha256=checksum, run_id=row["run_id"], kind=spec["kind"], role=spec["role"], metadata=spec["metadata"],
                        late=row["state"] == "uncertain" or row["current_attempt"] != attempt_id,
                        superseded=row["current_attempt"] != attempt_id)
            now = time.time()
            connection.execute("INSERT INTO results VALUES(?,?,?,?,?,?,NULL)", (result_id, attempt_id, row["job_id"], checksum, canonical(body), now))
            connection.execute("UPDATE attempts SET state=?,finished_at=? WHERE attempt_id=?", (result["outcome"], now, attempt_id))
            connection.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=? AND current_attempt=?", ("uncertain" if result["outcome"] == "uncertain" else "result_pending", now, row["job_id"], attempt_id))
            self._event(connection, "result_saved", row["job_id"], attempt_id, result_id=result_id, outcome=result["outcome"], late=body["late"])
            return result_id

    def pending_results(self, run_id=None):
        with closing(self._connect()) as connection:
            sql = "SELECT r.body FROM results r JOIN jobs j USING(job_id) WHERE r.acked_at IS NULL"
            args = []
            if run_id is not None:
                sql += " AND j.run_id=?"
                args.append(run_id)
            return [json.loads(row[0]) for row in connection.execute(sql + " ORDER BY r.created_at,r.result_id", args)]

    def ack_result(self, result_id):
        """Caller asserts semantic import is durable; repeated ACK is harmless."""
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM results WHERE result_id=?", (result_id,)).fetchone()
            if row is None:
                raise KeyError(result_id)
            if row["acked_at"] is not None:
                return False
            body = json.loads(row["body"])
            now = time.time()
            connection.execute("UPDATE results SET acked_at=? WHERE result_id=?", (now, result_id))
            connection.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=? AND current_attempt=?",
                ("uncertain" if body["outcome"] == "uncertain" else "acked", now, row["job_id"], row["attempt_id"]))
            self._event(connection, "result_acknowledged", row["job_id"], row["attempt_id"], result_id=result_id)
            return True

    def retry(self, job_id, allow_uncertain=False, reason="explicit retry"):
        with self._transaction() as connection:
            row = connection.execute("SELECT j.*,a.state attempt_state FROM jobs j LEFT JOIN attempts a ON a.attempt_id=j.current_attempt WHERE j.job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] == "queued":
                return job_id
            if row["state"] == "uncertain" and not allow_uncertain:
                raise QueueConflict("unknown external execution requires explicit allow_uncertain")
            if row["state"] != "uncertain" and row["attempt_state"] != "failed":
                raise QueueConflict("only failed or explicitly resolved uncertain attempts can retry")
            unacked = connection.execute("SELECT 1 FROM results WHERE job_id=? AND acked_at IS NULL", (job_id,)).fetchone()
            if unacked:
                raise QueueConflict("consume and acknowledge existing results before retry")
            connection.execute("UPDATE jobs SET state='queued',updated_at=? WHERE job_id=?", (time.time(), job_id))
            self._event(connection, "retry_authorized", job_id, row["current_attempt"], reason=reason, allow_uncertain=allow_uncertain)
        return job_id

    def cancel(self, job_id, reason="cancelled before dispatch"):
        with self._transaction() as connection:
            row = connection.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] != "queued":
                raise QueueConflict("only queued jobs may be cancelled; pause preserves running attempts")
            connection.execute("UPDATE jobs SET state='cancelled',updated_at=? WHERE job_id=?", (time.time(), job_id))
            self._event(connection, "cancelled", job_id, reason=reason)

    def jobs(self, run_id=None, *, include_spec=True):
        if self.read_only and not self.db_path.is_file(): return []
        with closing(self._connect()) as connection:
            fields = '*' if include_spec else 'job_id,run_id,state,current_attempt,created_at,updated_at'
            rows = connection.execute("SELECT " + fields + " FROM jobs" + (" WHERE run_id=?" if run_id is not None else "") + " ORDER BY created_at,job_id", (run_id,) if run_id is not None else ()).fetchall()
        return [dict(row, spec=json.loads(row["spec"])) if include_spec else dict(row) for row in rows]

    def attempts(self, job_id=None):
        if self.read_only and not self.db_path.is_file(): return []
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM attempts" + (" WHERE job_id=?" if job_id else "") + " ORDER BY created_at,attempt_id", (job_id,) if job_id else ()).fetchall()
        return [dict(row) for row in rows]

    def events(self):
        with closing(self._connect()) as connection:
            return [dict(row, data=json.loads(row["data"])) for row in connection.execute("SELECT * FROM execution_events ORDER BY sequence")]

    def status(self, run_id=None, *, details=True):
        if self.read_only and not self.db_path.is_file():
            return {"run_id":run_id,"counts":{},"paused":False if run_id else {},
                    "active":0,"uncertain":0,"queued":0,
                    **({"jobs":[],"attempts":[],"pending_results":[]} if details else {}),
                    "semantic_completion":"execution queue has not been initialized"}
        jobs = self.jobs(run_id, include_spec=details)
        counts = {}
        for job in jobs:
            counts[job["state"]] = counts.get(job["state"], 0) + 1
        with closing(self._connect()) as connection:
            controls = {row[0]: bool(row[1]) for row in connection.execute("SELECT * FROM controls")}
        job_ids = {job["job_id"] for job in jobs}
        attempts = [attempt for attempt in self.attempts() if attempt["job_id"] in job_ids] if details else []
        return {"run_id": run_id, "counts": counts, "paused": controls.get(run_id, False) if run_id else controls,
                **({"jobs": jobs, "attempts": attempts, "pending_results": self.pending_results(run_id)} if details else {}),
                "active": sum(counts.get(key, 0) for key in ("leased", "running")),
                "uncertain": counts.get("uncertain", 0), "queued": counts.get("queued", 0),
                "semantic_completion": "not determined by execution queue"}

    snapshot = status
