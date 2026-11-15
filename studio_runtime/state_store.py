"""Small mutable state plus immutable, content-addressed JSON bodies.

References are an internal storage format, never a model input. Lazy dictionaries
keep archival prompts/snapshots out of scheduling and status reads. Blob inserts
and the state pointer update use the same SQLite transaction.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3

REF = "$studio_body_v1"
LITERAL = "$studio_literal_v1"


class BodyRef(dict):
    """Only decoded storage pointers have this type; source JSON never does."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LazyDict(dict):
    def __init__(self, values, store):
        super().__init__(values)
        self.store = store
        self.loaded = {}

    def __iter__(self):
        # Disable CPython's exact-dict copying fast path for dict(x)/{**x}.
        return iter(dict.keys(self))

    def __eq__(self, other):
        return isinstance(other, dict) and set(self) == set(other) and all(self[k] == other[k] for k in self)

    def __ne__(self, other):
        return not self == other

    def clear(self):
        self.loaded.clear()
        dict.clear(self)

    def __getitem__(self, key):
        value = dict.__getitem__(self, key)
        if isinstance(value, BodyRef):
            if key not in self.loaded:
                self.loaded[key] = self.store.fetch(value[REF])
            return self.loaded[key]
        return value

    def get(self, key, default=None):
        return self[key] if key in self else default

    def __setitem__(self, key, value):
        self.loaded.pop(key, None)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self.loaded.pop(key, None)
        dict.__delitem__(self, key)

    def items(self):
        return ((k, self[k]) for k in self)

    def values(self):
        return (self[k] for k in self)

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        if key not in self:
            if default: return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items(): self[key] = value

    def __deepcopy__(self, memo):
        # Explicit snapshots/JobSpecs must remain usable after the transaction.
        value = {k: deepcopy(self[k], memo) for k in self}
        memo[id(self)] = value
        return value


class StateStore:
    def __init__(self, db, con):
        self.db, self.con = db, con
        self.read_bodies = 0
        self.inserted = set()

    @staticmethod
    def initialize(con):
        con.execute("CREATE TABLE IF NOT EXISTS state_bodies (sha256 TEXT PRIMARY KEY, body TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS artifact_publications (path TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE IF NOT EXISTS work_reports (job_id TEXT PRIMARY KEY, topic TEXT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL)")
        con.execute("CREATE INDEX IF NOT EXISTS work_reports_scope ON work_reports(topic,role)")
        con.execute("CREATE TABLE IF NOT EXISTS artifact_headers (path TEXT PRIMARY KEY, source_job TEXT NOT NULL, sha256 TEXT NOT NULL, chars INTEGER NOT NULL, bytes INTEGER NOT NULL)")
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='objects'").fetchone():
            con.execute("CREATE INDEX IF NOT EXISTS objects_kind ON objects(kind)")
            con.execute("CREATE INDEX IF NOT EXISTS objects_form ON objects(kind,json_extract(body,'$.form'))")

    def decode(self, value):
        if isinstance(value, dict):
            if set(value) == {REF}: return BodyRef(value)
            if set(value) == {LITERAL}:
                return LazyDict({k:self.decode(v) for k,v in value[LITERAL].items()},self)
            return LazyDict({k: self.decode(v) for k, v in value.items()}, self)
        if isinstance(value, list): return [self.fetch(v[REF]) if isinstance(v, dict) and set(v) == {REF} else self.decode(v) for v in value]
        return value

    def fetch(self, sha):
        try:
            row = self.con.execute("SELECT body FROM state_bodies WHERE sha256=?", (sha,)).fetchone()
        except sqlite3.ProgrammingError:
            with closing(sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)) as con:
                row = con.execute("SELECT body FROM state_bodies WHERE sha256=?", (sha,)).fetchone()
        if row is None: raise ValueError('missing archived state body: ' + sha)
        if hashlib.sha256(row[0].encode('utf-8')).hexdigest() != sha:
            raise ValueError('archived state body checksum mismatch: ' + sha)
        self.read_bodies += 1
        return self.decode(json.loads(row[0]))

    def blob(self, packed):
        body = encoded(packed)
        sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
        if sha not in self.inserted:
            self.con.execute("INSERT OR IGNORE INTO state_bodies VALUES(?,?)", (sha, body))
            self.inserted.add(sha)
        return BodyRef({REF: sha})

    def pack(self, value, path=()):
        if isinstance(value, dict):
            if isinstance(value, BodyRef): return value
            if set(value) in ({REF},{LITERAL}):
                return {LITERAL:{k:self.pack(value[k],path+(k,)) for k in value}}
            packed = {}
            for k in value:
                if isinstance(value, LazyDict) and k not in value.loaded:
                    raw = dict.__getitem__(value, k)
                    if isinstance(raw, BodyRef):
                        packed[k] = raw
                        continue
                item = self.pack(value[k], path + (k,))
                # Full task snapshots are immutable and shared by digest. Large
                # job payloads are not scheduling metadata.
                archive = (path and path[-1] in {'context_snapshots', 'obligation_catalogs'} or
                           k == 'schema' and isinstance(item, dict) or
                           k in {'prompt', 'output_schema', 'user_context', 'operation_packet',
                                 'selection', 'source_manifest', 'omitted_catalog',
                                 'capacity', 'input_accounting', 'cognitive_protocol', 'input_packet',
                                 'context_projection', 'memory_context', 'semantic_context'})
                packed[k] = self.blob(item) if archive and not isinstance(item, BodyRef) else item
            return packed
        if isinstance(value, list): return [self.pack(v, path + (str(i),)) for i, v in enumerate(value)]
        if isinstance(value, str) and len(value) >= 4096: return self.blob(value)
        return value

    def load(self):
        return self.decode(json.loads(self.con.execute("SELECT body FROM workflow WHERE key='state'").fetchone()[0]))

    def save(self, state):
        body = encoded(self.pack(state))
        # Polls without state transitions do not write or inflate the WAL.
        self.con.execute("UPDATE workflow SET body=? WHERE key='state' AND body<>?", (body, body))


def read_state(con):
    from pathlib import Path
    path = Path(con.execute('PRAGMA database_list').fetchone()[2]).resolve()
    return StateStore(path, con).load()


def write_state(con, state):
    from pathlib import Path
    path = Path(con.execute('PRAGMA database_list').fetchone()[2]).resolve()
    StateStore.initialize(con)
    StateStore(path, con).save(state)
