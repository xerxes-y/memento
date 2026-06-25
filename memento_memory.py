#!/usr/bin/env python3
"""memento_memory — memento's own memory engine (stdlib-only, no external deps).

A self-contained, agentmemory-inspired memory system **owned by the memento
project**. Standard library only (sqlite3 + http.server + math/hashlib), so it
runs anywhere memento runs — including inside an isolated `uvx` environment.

Implemented across phases, all on one SQLite schema:

  Phase 1  store + BM25 full-text search + tiers + secret redaction +
           agentmemory-compatible export + local web dashboard
  Phase 2  vector search (term-frequency bag-of-words embeddings) fused with
           BM25 via Reciprocal Rank Fusion (hybrid retrieval)
  Phase 3  knowledge graph — entity extraction, mem<->entity links, related-memory
           traversal, graph API for the dashboard
  Phase 4  lifecycle — decay scoring, tier auto-consolidation/auto-forget, and
           12 capture hooks that turn agent events into working memories
  Phase 5  governance — namespaces (isolated/shared), an audit log, and
           git-versionable snapshot / restore

Honest scope: embeddings are deterministic term-frequency bag-of-words vectors
(real vector-space cosine, no model/API needed — for true synonym-level semantics
swap in a neural embedder via `Embedder`). Entity extraction is heuristic. This
is agentmemory-*class* core coverage, not a byte-for-byte clone of its 53-tool
surface.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── configuration ─────────────────────────────────────────────────────────────

TIERS = ("working", "episodic", "semantic", "procedural")
DEFAULT_TIER = "episodic"
DEFAULT_NAMESPACE = "default"

CAPTURE_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "PreCompact", "SubagentStart", "SubagentStop",
    "Stop", "SessionEnd", "Notification", "Error",
)

RRF_K = 60               # reciprocal rank fusion constant
VEC_MIN = 0.05           # min cosine for a vector-only hit to count
WORKING_TTL_S = 86_400   # un-accessed working memories older than this are forgotten
PROMOTE_ACCESS = 3       # access count that promotes a memory up a tier
DECAY_HALFLIFE_S = 7 * 86_400

DEFAULT_DB = os.environ.get(
    "MEMENTO_MEMORY_DB", os.path.expanduser("~/.memento/memory.db"))
DEFAULT_EXPORT = os.environ.get(
    "MEMENTO_MEMORY_PATH", os.path.expanduser("~/.agentmemory/standalone.json"))
DEFAULT_PORT = int(os.environ.get("MEMENTO_DASHBOARD_PORT", "3114"))

# ── secret redaction (privacy filtering before storage) ───────────────────────

_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bpypi-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:secret|token|password|api[_-]?key)\s*[=:]\s*\S+"),
]


def redact_secrets(text: str) -> str:
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


# ── embeddings (Phase 2): deterministic hashing-TF vectors, swappable ─────────

class Embedder:
    """Term-frequency embedder → sparse, L2-normalized bag-of-words vector.

    Keys are the tokens themselves (collision-free, deterministic) — a genuine
    vector-space model, no model or network needed. Subclass and override
    ``embed`` to plug in a neural/semantic embedder later without touching the
    rest of the engine.
    """

    @staticmethod
    def _tokens(text):
        return [t for t in re.split(r"\W+", (text or "").lower()) if len(t) > 1]

    def embed(self, text) -> dict:
        vec = {}
        for tok in self._tokens(text):
            vec[tok] = vec.get(tok, 0.0) + 1.0
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) < len(b) else (b, a)
    return sum(w * big.get(i, 0.0) for i, w in small.items())


def _fts_query(raw: str) -> str:
    terms = [t for t in re.split(r"\W+", raw or "") if t]
    return " ".join('"%s"' % t for t in terms)


# ── entity extraction (Phase 3) ───────────────────────────────────────────────

_ENTITY_PATTERNS = [
    re.compile(r"`([^`]{2,60})`"),                       # `backticked`
    re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:[A-Z][a-zA-Z0-9]+)+)\b"),  # CamelCase
    re.compile(r"\b([a-zA-Z_][\w/]*\.[a-zA-Z][\w./]*)\b"),         # dotted/paths
]


_ENTITY_STOP = {"redacted"}


def extract_entities(text: str) -> list:
    found = {}
    for pat in _ENTITY_PATTERNS:
        for m in pat.findall(text or ""):
            name = m.strip().strip("`")
            if 2 <= len(name) <= 60 and name.lower() not in _ENTITY_STOP:
                found[name.lower()] = name
    return list(found.values())


# ── the store ─────────────────────────────────────────────────────────────────

class MemoryStore:
    def __init__(self, db_path=None, export_path=None, embedder=None):
        self.db_path = db_path or DEFAULT_DB
        self.export_path = export_path or DEFAULT_EXPORT
        self.embedder = embedder or Embedder()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.fts = True
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS memories(
                    id TEXT PRIMARY KEY,
                    tier TEXT NOT NULL DEFAULT 'episodic',
                    namespace TEXT NOT NULL DEFAULT 'default',
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    session TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    actor TEXT NOT NULL DEFAULT '',
                    created_ts REAL NOT NULL,
                    accessed_ts REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0
                )""")
            c.execute("""CREATE TABLE IF NOT EXISTS mem_entities(
                    mem_id TEXT NOT NULL, entity TEXT NOT NULL,
                    PRIMARY KEY (mem_id, entity))""")
            c.execute("""CREATE TABLE IF NOT EXISTS audit(
                    ts REAL NOT NULL, op TEXT NOT NULL,
                    mem_id TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '')""")
            try:
                c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                    USING fts5(id UNINDEXED, title, content, tags)""")
            except sqlite3.OperationalError:
                self.fts = False

    # ── audit ───────────────────────────────────────────────────────────────

    def _audit(self, c, op, mem_id="", actor="", detail=""):
        c.execute("INSERT INTO audit(ts,op,mem_id,actor,detail) VALUES(?,?,?,?,?)",
                  (time.time(), op, mem_id, actor, detail))

    def audit_log(self, limit=50) -> list:
        with self._connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (int(limit),))]

    # ── writes ──────────────────────────────────────────────────────────────

    @staticmethod
    def _norm_tags(tags):
        if isinstance(tags, (list, tuple)):
            return ",".join(t.strip() for t in tags if str(t).strip())
        return str(tags or "").strip()

    def save(self, title, content, tier=None, tags=None, session="",
             source="manual", namespace=DEFAULT_NAMESPACE, actor="") -> str:
        title = redact_secrets((title or "").strip())
        content = redact_secrets((content or "").strip())
        if not title or not content:
            raise ValueError("both 'title' and 'content' are required")
        tier = tier if tier in TIERS else DEFAULT_TIER
        tags = self._norm_tags(tags)
        namespace = namespace or DEFAULT_NAMESPACE
        mem_id = "mem-" + hashlib.sha1(
            (namespace + "\x00" + title + "\x00" + content).encode()).hexdigest()[:12]
        now = time.time()
        with self._connect() as c:
            c.execute("""
                INSERT INTO memories(id,tier,namespace,title,content,tags,session,
                                     source,actor,created_ts,accessed_ts,access_count,pinned)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0)
                ON CONFLICT(id) DO UPDATE SET tier=excluded.tier, tags=excluded.tags,
                    session=excluded.session, accessed_ts=excluded.accessed_ts
            """, (mem_id, tier, namespace, title, content, tags, session, source,
                  actor, now, now))
            if self.fts:
                c.execute("DELETE FROM memories_fts WHERE id=?", (mem_id,))
                c.execute("INSERT INTO memories_fts(id,title,content,tags) "
                          "VALUES(?,?,?,?)", (mem_id, title, content, tags))
            c.execute("DELETE FROM mem_entities WHERE mem_id=?", (mem_id,))
            for ent in extract_entities(title + " " + content):
                c.execute("INSERT OR IGNORE INTO mem_entities(mem_id,entity) "
                          "VALUES(?,?)", (mem_id, ent))
            self._audit(c, "save", mem_id, actor, tier)
        self._export()
        return mem_id

    def capture(self, event_type, payload, session="", namespace=DEFAULT_NAMESPACE,
                actor="agent") -> str:
        """Phase 4 hook: turn a lifecycle event into a working-tier memory."""
        et = event_type if event_type in CAPTURE_EVENTS else (event_type or "Event")
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return self.save(f"[{et}] {body[:60]}", body or et, tier="working",
                         session=session, source="hook", namespace=namespace,
                         actor=actor)

    def pin(self, mem_id, pinned=True) -> bool:
        with self._connect() as c:
            cur = c.execute("UPDATE memories SET pinned=? WHERE id=?",
                            (1 if pinned else 0, mem_id))
            self._audit(c, "pin" if pinned else "unpin", mem_id)
            return cur.rowcount > 0

    def forget(self, mem_id=None, query=None) -> int:
        with self._connect() as c:
            if mem_id:
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM memories WHERE id=?", (mem_id,))]
            elif query:
                ids = [m["id"] for m in self.search(query, limit=1000)]
            else:
                return 0
            for i in ids:
                c.execute("DELETE FROM memories WHERE id=?", (i,))
                c.execute("DELETE FROM mem_entities WHERE mem_id=?", (i,))
                if self.fts:
                    c.execute("DELETE FROM memories_fts WHERE id=?", (i,))
                self._audit(c, "forget", i)
        self._export()
        return len(ids)

    # ── reads ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _row(r):
        return {k: r[k] for k in r.keys()}

    def get(self, mem_id):
        with self._connect() as c:
            r = c.execute("SELECT * FROM memories WHERE id=?", (mem_id,)).fetchone()
            return self._row(r) if r else None

    def list(self, limit=20, tier=None, session=None, namespace=None):
        sql, clauses, params = "SELECT * FROM memories", [], []
        if tier in TIERS:
            clauses.append("tier=?"); params.append(tier)
        if session:
            clauses.append("session=?"); params.append(session)
        if namespace:
            clauses.append("namespace=?"); params.append(namespace)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_ts DESC LIMIT ?"; params.append(int(limit))
        with self._connect() as c:
            return [self._row(r) for r in c.execute(sql, params)]

    def _bm25_ids(self, c, query, tier, namespace):
        extra = (" AND m.tier=?" if tier in TIERS else "") + \
                (" AND m.namespace=?" if namespace else "")
        params_tail = ([tier] if tier in TIERS else []) + \
                      ([namespace] if namespace else [])
        if self.fts:
            match = _fts_query(query)
            if match:
                rows = c.execute(
                    "SELECT m.id FROM memories_fts f JOIN memories m ON m.id=f.id "
                    "WHERE memories_fts MATCH ?" + extra +
                    " ORDER BY bm25(memories_fts)", [match] + params_tail)
                return [r["id"] for r in rows]
        like = f"%{query}%"
        rows = c.execute(
            "SELECT id FROM memories m WHERE (title LIKE ? OR content LIKE ? "
            "OR tags LIKE ?)" + extra + " ORDER BY created_ts DESC",
            [like, like, like] + params_tail)
        return [r["id"] for r in rows]

    def _vector_ids(self, c, query, tier, namespace):
        qv = self.embedder.embed(query)
        if not qv:
            return []
        clauses, params = [], []
        if tier in TIERS:
            clauses.append("tier=?"); params.append(tier)
        if namespace:
            clauses.append("namespace=?"); params.append(namespace)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        scored = []
        for r in c.execute("SELECT id,title,content FROM memories" + where, params):
            sim = cosine(qv, self.embedder.embed(r["title"] + " " + r["content"]))
            if sim >= VEC_MIN:
                scored.append((sim, r["id"]))
        scored.sort(reverse=True)
        return [i for _, i in scored]

    def search(self, query, limit=10, tier=None, namespace=None, mode="hybrid"):
        """Retrieve memories. mode: 'bm25' | 'vector' | 'hybrid' (RRF fusion)."""
        q = (query or "").strip()
        if not q:
            return self.list(limit=limit, tier=tier, namespace=namespace)
        with self._connect() as c:
            bm = self._bm25_ids(c, q, tier, namespace) if mode in ("bm25", "hybrid") else []
            vec = self._vector_ids(c, q, tier, namespace) if mode in ("vector", "hybrid") else []
            if mode == "bm25":
                order = bm
            elif mode == "vector":
                order = vec
            else:
                scores = {}
                for rank, i in enumerate(bm):
                    scores[i] = scores.get(i, 0.0) + 1.0 / (RRF_K + rank)
                for rank, i in enumerate(vec):
                    scores[i] = scores.get(i, 0.0) + 1.0 / (RRF_K + rank)
                order = [i for i, _ in sorted(scores.items(),
                                              key=lambda kv: kv[1], reverse=True)]
            order = order[:int(limit)]
            rows = {r["id"]: self._row(r) for r in c.execute(
                "SELECT * FROM memories WHERE id IN (%s)" %
                ",".join("?" * len(order)), order)} if order else {}
            self._touch(c, order)
            return [rows[i] for i in order if i in rows]

    @staticmethod
    def _touch(c, ids):
        now = time.time()
        for i in ids:
            c.execute("UPDATE memories SET accessed_ts=?, access_count=access_count+1 "
                      "WHERE id=?", (now, i))

    # ── knowledge graph (Phase 3) ─────────────────────────────────────────────

    def related(self, mem_id, limit=10) -> list:
        """Memories sharing an entity with the given memory."""
        with self._connect() as c:
            ents = [r["entity"] for r in c.execute(
                "SELECT entity FROM mem_entities WHERE mem_id=?", (mem_id,))]
            if not ents:
                return []
            rows = c.execute(
                "SELECT m.*, COUNT(*) AS shared FROM mem_entities e "
                "JOIN memories m ON m.id=e.mem_id "
                "WHERE e.entity IN (%s) AND e.mem_id<>? "
                "GROUP BY m.id ORDER BY shared DESC, m.created_ts DESC LIMIT ?"
                % ",".join("?" * len(ents)), ents + [mem_id, int(limit)])
            return [self._row(r) for r in rows]

    def graph(self, limit=40) -> dict:
        """Entities, the memories they touch, and edges — for the graph viz."""
        with self._connect() as c:
            ents = c.execute(
                "SELECT entity, COUNT(*) AS n FROM mem_entities "
                "GROUP BY entity ORDER BY n DESC LIMIT ?", (int(limit),)).fetchall()
            names = [e["entity"] for e in ents]
            edges, mem_ids = [], set()
            if names:
                for r in c.execute(
                        "SELECT mem_id, entity FROM mem_entities WHERE entity IN (%s)"
                        % ",".join("?" * len(names)), names):
                    edges.append({"mem": r["mem_id"], "entity": r["entity"]})
                    mem_ids.add(r["mem_id"])
            mems = []
            if mem_ids:
                for r in c.execute(
                        "SELECT id,title,tier FROM memories WHERE id IN (%s)"
                        % ",".join("?" * len(mem_ids)), list(mem_ids)):
                    mems.append({"id": r["id"], "title": r["title"], "tier": r["tier"]})
            return {"entities": [{"name": e["entity"], "count": e["n"]} for e in ents],
                    "memories": mems, "edges": edges}

    # ── lifecycle: decay + consolidation (Phase 4) ────────────────────────────

    @staticmethod
    def decay_score(mem, now=None) -> float:
        if mem.get("pinned"):
            return 1.0
        now = now if now is not None else time.time()
        age = max(0.0, now - mem.get("accessed_ts", now))
        recency = math.exp(-age / DECAY_HALFLIFE_S)
        reinforce = 1.0 + math.log1p(mem.get("access_count", 0))
        return recency * reinforce

    def consolidate(self, now=None) -> dict:
        """Promote reinforced memories up a tier; forget stale working memories."""
        now = now if now is not None else time.time()
        promoted = forgotten = 0
        with self._connect() as c:
            for r in c.execute("SELECT * FROM memories").fetchall():
                m = self._row(r)
                if m["pinned"]:
                    continue
                if (m["tier"] == "working" and m["access_count"] == 0
                        and now - m["created_ts"] > WORKING_TTL_S):
                    c.execute("DELETE FROM memories WHERE id=?", (m["id"],))
                    c.execute("DELETE FROM mem_entities WHERE mem_id=?", (m["id"],))
                    if self.fts:
                        c.execute("DELETE FROM memories_fts WHERE id=?", (m["id"],))
                    self._audit(c, "consolidate-forget", m["id"])
                    forgotten += 1
                elif m["access_count"] >= PROMOTE_ACCESS and m["tier"] in ("working", "episodic"):
                    nxt = "episodic" if m["tier"] == "working" else "semantic"
                    c.execute("UPDATE memories SET tier=? WHERE id=?", (nxt, m["id"]))
                    self._audit(c, "consolidate-promote", m["id"], detail=nxt)
                    promoted += 1
        self._export()
        return {"promoted": promoted, "forgotten": forgotten}

    # ── lessons: pattern detection → derived insights (semantic tier) ─────────

    def patterns(self, min_support=2, namespace=None) -> dict:
        """Detect recurring signals across non-lesson memories."""
        ns = " AND m.namespace=?" if namespace else ""
        nsp = [namespace] if namespace else []
        with self._connect() as c:
            ents = [(r["entity"], r["n"]) for r in c.execute(
                "SELECT e.entity, COUNT(*) AS n FROM mem_entities e "
                "JOIN memories m ON m.id=e.mem_id "
                "WHERE m.source<>'lesson'" + ns +
                " GROUP BY e.entity HAVING n>=? ORDER BY n DESC",
                nsp + [min_support])]
            tagc = {}
            for r in c.execute(
                    "SELECT tags FROM memories m WHERE source<>'lesson'" + ns, nsp):
                for t in (r["tags"] or "").split(","):
                    t = t.strip()
                    if t:
                        tagc[t] = tagc.get(t, 0) + 1
            tags = [(t, n) for t, n in sorted(tagc.items(), key=lambda x: -x[1])
                    if n >= min_support]
            fails = [self._row(r) for r in c.execute(
                "SELECT * FROM memories WHERE source='hook' AND "
                "(title LIKE '%Failure%' OR content LIKE '%fail%' "
                "OR content LIKE '%error%')")]
        return {"entities": ents, "tags": tags, "failures": fails}

    def learn(self, min_support=2, namespace=DEFAULT_NAMESPACE) -> list:
        """Derive lessons from patterns into the semantic tier.

        Heuristic + stdlib: summarizes recurring entities, tags, and failures.
        Regenerates auto-lessons each run (pinned lessons are kept). Swap in an
        LLM to synthesize richer lessons without changing the rest of the engine.
        """
        p = self.patterns(min_support, None if namespace == DEFAULT_NAMESPACE else namespace)
        with self._connect() as c:  # clear prior unpinned auto-lessons
            for i in [r["id"] for r in c.execute(
                    "SELECT id FROM memories WHERE source='lesson' AND pinned=0")]:
                c.execute("DELETE FROM memories WHERE id=?", (i,))
                c.execute("DELETE FROM mem_entities WHERE mem_id=?", (i,))
                if self.fts:
                    c.execute("DELETE FROM memories_fts WHERE id=?", (i,))
        created = []
        for ent, n in p["entities"][:10]:
            mid = self.save(
                f"Lesson: recurring focus on {ent}",
                f"'{ent}' recurs across {n} memories — a stable topic; consider a "
                f"dedicated SKILL.md section or guardrail for it.",
                tier="semantic", tags="lesson", source="lesson", namespace=namespace)
            created.append((mid, ent))
        if p["failures"]:
            eg = ", ".join(sorted({f["title"] for f in p["failures"]})[:5])
            mid = self.save(
                "Lesson: recurring failures",
                f"{len(p['failures'])} failure events captured (e.g. {eg}). "
                f"Find the root cause and encode a check so it does not recur.",
                tier="semantic", tags="lesson", source="lesson", namespace=namespace)
            created.append((mid, "failures"))
        return created

    def lessons(self, limit=20) -> list:
        with self._connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM memories WHERE source='lesson' "
                "ORDER BY pinned DESC, created_ts DESC LIMIT ?", (int(limit),))]

    def add_lesson(self, title, content) -> str:
        """Manually author a lesson. Pinned, so memory_learn never wipes it."""
        mid = self.save(title, content, tier="semantic", tags="lesson",
                        source="lesson")
        self.pin(mid, True)
        return mid

    def sessions(self) -> list:
        with self._connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT session, COUNT(*) AS n, MAX(created_ts) AS last "
                "FROM memories WHERE session<>'' GROUP BY session ORDER BY last DESC")]

    # ── governance: namespaces + snapshots (Phase 5) ──────────────────────────

    def namespaces(self) -> list:
        with self._connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT namespace, COUNT(*) AS n FROM memories "
                "GROUP BY namespace ORDER BY n DESC")]

    def snapshot(self, path) -> int:
        with self._connect() as c:
            mems = [self._row(r) for r in c.execute("SELECT * FROM memories")]
            ents = [self._row(r) for r in c.execute("SELECT * FROM mem_entities")]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "memories": mems, "mem_entities": ents},
                      f, indent=2)
        return len(mems)

    def restore(self, path) -> int:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        mems = data.get("memories", [])
        with self._connect() as c:
            for m in mems:
                cols = ("id,tier,namespace,title,content,tags,session,source,actor,"
                        "created_ts,accessed_ts,access_count,pinned")
                c.execute(
                    "INSERT OR REPLACE INTO memories(%s) VALUES(%s)"
                    % (cols, ",".join("?" * 13)),
                    [m.get("id"), m.get("tier", "episodic"),
                     m.get("namespace", "default"), m.get("title", ""),
                     m.get("content", ""), m.get("tags", ""), m.get("session", ""),
                     m.get("source", "restore"), m.get("actor", ""),
                     m.get("created_ts", time.time()),
                     m.get("accessed_ts", time.time()),
                     m.get("access_count", 0), m.get("pinned", 0)])
                if self.fts:
                    c.execute("DELETE FROM memories_fts WHERE id=?", (m.get("id"),))
                    c.execute("INSERT INTO memories_fts(id,title,content,tags) "
                              "VALUES(?,?,?,?)", (m.get("id"), m.get("title", ""),
                                                  m.get("content", ""), m.get("tags", "")))
            for e in data.get("mem_entities", []):
                c.execute("INSERT OR IGNORE INTO mem_entities(mem_id,entity) "
                          "VALUES(?,?)", (e.get("mem_id"), e.get("entity")))
            self._audit(c, "restore", detail=os.path.basename(path))
        self._export()
        return len(mems)

    # ── stats + agentmemory-compatible export ─────────────────────────────────

    def stats(self) -> dict:
        with self._connect() as c:
            total = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            by_tier = {r["tier"]: r["n"] for r in c.execute(
                "SELECT tier, COUNT(*) AS n FROM memories GROUP BY tier")}
            entities = c.execute(
                "SELECT COUNT(DISTINCT entity) FROM mem_entities").fetchone()[0]
            namespaces = c.execute(
                "SELECT COUNT(DISTINCT namespace) FROM memories").fetchone()[0]
        return {"total": total, "by_tier": by_tier, "entities": entities,
                "namespaces": namespaces, "fts": self.fts, "db": self.db_path}

    def _export(self):
        try:
            os.makedirs(os.path.dirname(self.export_path) or ".", exist_ok=True)
            mems = {}
            with self._connect() as c:
                for r in c.execute(
                        "SELECT id,title,content FROM memories ORDER BY created_ts"):
                    mems[r["id"]] = {"title": r["title"], "content": r["content"]}
            tmp = self.export_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"mem:memories": mems}, f, indent=2)
            os.replace(tmp, self.export_path)
        except OSError:
            pass


# ── local web dashboard (stdlib http.server, vanilla JS) ──────────────────────

_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>memento · memory</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#e9d5ff;
 background:linear-gradient(160deg,#0b1022,#1a1240 55%,#3b1a78);min-height:100vh;display:flex}
aside{width:210px;flex:none;padding:18px 14px;border-right:1px solid #ffffff14;
 background:#0b1022aa;backdrop-filter:blur(6px);position:sticky;top:0;height:100vh}
aside h1{font-size:17px;margin:0 0 18px;color:#f8fafc;letter-spacing:1px}
nav button{display:block;width:100%;text-align:left;margin:4px 0;padding:9px 12px;border:0;border-radius:10px;
 background:transparent;color:#c4b5fd;cursor:pointer;font:inherit}
nav button.on{background:#7c3aed33;color:#f8fafc;box-shadow:inset 0 0 0 1px #7c3aed66}
nav button:hover{background:#ffffff10}
aside .foot{position:absolute;bottom:16px;font-size:11px;color:#8b7fb8;width:182px;word-break:break-all}
main{flex:1;padding:22px 28px;max-width:980px}
h2{margin:0 0 4px;color:#f8fafc;font-size:22px;letter-spacing:.5px}
.sub{color:#a895d8;margin:0 0 18px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.stat{background:#ffffff0e;border:1px solid #ffffff1f;border-radius:14px;padding:14px 16px}
.stat .n{font-size:26px;font-weight:700;color:#f8fafc}.stat .l{font-size:12px;color:#a895d8}
.card{background:#ffffff0e;border:1px solid #ffffff1f;border-radius:14px;padding:14px 16px;margin:12px 0}
.card h3{margin:0 0 4px;color:#f8fafc;font-size:15px}
.bar{height:10px;border-radius:6px;background:#ffffff14;overflow:hidden;margin:6px 0}
.bar>span{display:block;height:100%}
input,textarea,select,button{font:inherit;border-radius:10px;border:1px solid #ffffff2b;background:#ffffff10;color:#f8fafc;padding:9px 12px}
input,textarea{width:100%}.row{display:flex;gap:10px;margin:8px 0}.row>*{flex:1}
.btn{cursor:pointer;background:#7c3aed;border-color:#7c3aed;font-weight:600}
.btn.ghost{background:#ffffff12;border-color:#ffffff2b}
.meta{font-size:12px;color:#c4b5fd99;margin-top:8px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.badge{font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-radius:99px;padding:2px 9px;font-weight:600}
.act{color:#fca5a5;cursor:pointer;font-size:12px}.act.pin{color:#fcd34d}
.chip{display:inline-block;background:#ffffff14;border:1px solid #ffffff22;border-radius:99px;padding:3px 11px;margin:3px;cursor:pointer;font-size:13px}
#cv{width:100%;height:62vh;background:#0b102255;border:1px solid #ffffff1f;border-radius:14px;display:block}
#gtip{position:fixed;pointer-events:none;background:#0b1022ee;border:1px solid #7c3aed;border-radius:8px;padding:4px 8px;font-size:12px;display:none;max-width:280px}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#7c3aed;color:#fff;
 padding:10px 16px;border-radius:10px;opacity:0;transition:.3s;pointer-events:none}
.feed{font-size:13px}.feed div{padding:6px 0;border-bottom:1px solid #ffffff12;display:flex;gap:10px}
.feed .op{color:#67e8f9;min-width:150px}
</style></head><body>
<aside>
 <h1>🌙 memento</h1>
 <nav id=nav>
  <button data-v=overview class=on>Overview</button>
  <button data-v=memories>Memories</button>
  <button data-v=graph>Knowledge graph</button>
  <button data-v=lessons>Lessons</button>
  <button data-v=sessions>Sessions</button>
  <button data-v=activity>Activity</button>
 </nav>
 <div class=foot id=foot></div>
</aside>
<main id=main></main>
<div id=gtip></div><div id=toast></div>
<script>
const TC={working:'#22d3ee',episodic:'#a78bfa',semantic:'#34d399',procedural:'#f59e0b',lesson:'#f472b6'};
const $=s=>document.querySelector(s);
const esc=x=>(x||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const api=async p=>(await fetch(p)).json();
const post=async(p,b)=>(await fetch(p,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b||{})})).json();
function toast(m){const t=$('#toast');t.textContent=m;t.style.opacity=1;setTimeout(()=>t.style.opacity=0,1800);}
function badge(t){return `<span class=badge style="background:${(TC[t]||'#888')}22;color:${TC[t]||'#aaa'}">${t}</span>`;}

function nav(v){document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('on',b.dataset.v==v));
 ({overview:overview,memories:memories,graph:graphView,lessons:lessons,sessions:sessions,activity:activity}[v]||overview)();}
document.querySelectorAll('#nav button').forEach(b=>b.onclick=()=>location.hash=b.dataset.v);
window.onhashchange=()=>nav(location.hash.slice(1)||'overview');

async function overview(){
 const s=await api('/api/stats'),a=(await api('/api/audit')).audit.slice(0,8);
 const tiers=['working','episodic','semantic','procedural'];
 const mx=Math.max(1,...tiers.map(t=>s.by_tier[t]||0));
 $('#main').innerHTML=`<h2>Overview</h2><p class=sub>health & activity at a glance</p>
 <div class=grid>
  <div class=stat><div class=n>${s.total}</div><div class=l>memories</div></div>
  <div class=stat><div class=n>${s.entities}</div><div class=l>graph entities</div></div>
  <div class=stat><div class=n>${s.namespaces}</div><div class=l>namespaces</div></div>
  <div class=stat><div class=n>${s.fts?'BM25':'LIKE'}</div><div class=l>+ vector search</div></div>
 </div>
 <div class=card><h3>Memory tiers</h3>${tiers.map(t=>`<div class=meta style=margin:10px_0>
   ${badge(t)}<div class=bar style=flex:1><span style="width:${(s.by_tier[t]||0)/mx*100}%;background:${TC[t]}"></span></div>
   <span>${s.by_tier[t]||0}</span></div>`).join('')}</div>
 <div class=card><h3>Quick actions</h3>
  <button class=btn onclick="act('/api/learn','Lessons derived')">✨ Learn lessons</button>
  <button class="btn ghost" onclick="act('/api/consolidate','Consolidated')">♻️ Consolidate</button></div>
 <div class=card><h3>Recent activity</h3><div class=feed>${a.map(r=>`<div>
   <span class=op>${esc(r.op)}</span><span>${esc(r.mem_id||r.detail||'')}</span></div>`).join('')||'nothing yet'}</div></div>`;
}
async function act(p,msg){await post(p,{});toast(msg);overview();}

async function memories(){
 $('#main').innerHTML=`<h2>Memories</h2><p class=sub>hybrid search · BM25 + semantic vector</p>
 <div class=row><input id=q placeholder="Search…" oninput=loadMem()>
  <select id=ft onchange=loadMem() style=max-width:160px><option value="">all tiers</option>
   <option>working<option>episodic<option>semantic<option>procedural</select></div>
 <details class=card><summary style=cursor:pointer>+ Add a memory</summary>
  <div class=row><input id=mt placeholder=Title></div><textarea id=mc rows=3 placeholder=Content></textarea>
  <div class=row><select id=mtier><option>episodic<option>working<option>semantic<option>procedural</select>
   <input id=mtags placeholder=tags><button class=btn onclick=saveMem()>Save</button></div></details>
 <div id=list></div>`;
 loadMem();
}
async function loadMem(){
 const d=await api('/api/memories?q='+encodeURIComponent($('#q').value)+'&tier='+($('#ft').value||''));
 $('#list').innerHTML=d.memories.map(m=>`<div class=card>
  <span class=act onclick="mforget('${m.id}')">forget ✕</span>
  <span class="act pin" onclick="mpin('${m.id}',${m.pinned?0:1})">${m.pinned?'📌 pinned':'pin'}</span>
  <h3>${esc(m.title)}</h3><div>${esc(m.content)}</div>
  <div class=meta>${badge(m.tier)}<span>${esc(m.namespace)}</span>${m.tags?'<span>#'+esc(m.tags)+'</span>':''}
   <span>${new Date(m.created_ts*1000).toLocaleString()}</span></div></div>`).join('')||'<p class=sub>No memories.</p>';
 setFoot();
}
async function saveMem(){const r=await post('/api/memories',{title:$('#mt').value,content:$('#mc').value,tier:$('#mtier').value,tags:$('#mtags').value});
 if(r.error){toast(r.error);return;}$('#mt').value=$('#mc').value=$('#mtags').value='';toast('saved');loadMem();}
async function mforget(id){await post('/api/forget',{id});toast('forgotten');loadMem();}
async function mpin(id,p){await post('/api/pin',{id,pinned:!!p});loadMem();}

async function lessons(){
 const d=await api('/api/lessons');
 $('#main').innerHTML=`<h2>Lessons</h2><p class=sub>insights derived from recurring patterns — or pin your own</p>
 <div class=card><button class=btn onclick=doLearn()>✨ Learn now</button>
  <span class=sub style=margin-left:10px>mines recurring entities, tags & failures into the semantic tier</span></div>
 <details class=card><summary style=cursor:pointer>+ Add a lesson (manual, pinned)</summary>
  <div class=row><input id=lt placeholder="Lesson title"></div>
  <textarea id=lc rows=2 placeholder="What did we learn?"></textarea>
  <div class=row><button class=btn onclick=doAddLesson()>Save lesson</button></div>
  <span class=sub>Manual lessons are pinned 📌, so "Learn now" never overwrites them.</span></details>
 <div id=ll>${d.lessons.map(m=>`<div class=card>${badge('lesson')} ${m.pinned?'📌 ':''}<h3 style=display:inline>${esc(m.title)}</h3>
   <div style=margin-top:6px>${esc(m.content)}</div></div>`).join('')||'<p class=sub>No lessons yet — click Learn now or add one.</p>'}</div>`;
}
async function doLearn(){const r=await post('/api/learn',{});toast('derived '+(r.created||[]).length+' lesson(s)');lessons();}
async function doAddLesson(){const r=await post('/api/lesson',{title:$('#lt').value,content:$('#lc').value});
 if(r.error){toast(r.error);return;}toast('lesson pinned');lessons();}

async function sessions(){
 const d=await api('/api/sessions');
 $('#main').innerHTML=`<h2>Sessions</h2><p class=sub>memories grouped by session</p>
 ${d.sessions.map(s=>`<div class=card><h3>${esc(s.session)}</h3>
   <div class=meta><span>${s.n} memories</span><span>last ${new Date(s.last*1000).toLocaleString()}</span></div></div>`).join('')||'<p class=sub>No sessions yet.</p>'}`;
}

async function activity(){
 const d=await api('/api/audit');
 $('#main').innerHTML=`<h2>Activity</h2><p class=sub>audit log — every operation</p>
 <div class=card><div class=feed>${d.audit.map(r=>`<div><span class=op>${esc(r.op)}</span>
   <span>${esc(r.mem_id||'')}</span><span style=color:#a895d8>${esc(r.detail||'')}</span></div>`).join('')||'empty'}</div></div>`;
}

// ── force-directed knowledge graph ────────────────────────────────────────────
let RAF=null;
async function graphView(){
 $('#main').innerHTML=`<h2>Knowledge graph</h2><p class=sub>entities ◯ and the memories ● that mention them · drag nodes</p>
  <canvas id=cv></canvas>`;
 const g=await api('/api/graph');
 const cv=$('#cv');cv.width=cv.clientWidth;cv.height=cv.clientHeight;
 const ents=g.entities.map(e=>({id:'e:'+e.name,label:e.name,type:'e',r:9+Math.min(16,e.count*3)}));
 const mems=g.memories.map(m=>({id:'m:'+m.id,label:m.title,type:'m',tier:m.tier,r:6}));
 const nodes=ents.concat(mems),idx={};nodes.forEach((n,i)=>idx[n.id]=i);
 nodes.forEach(n=>{n.x=Math.random()*cv.width;n.y=Math.random()*cv.height;n.vx=0;n.vy=0;});
 const links=g.edges.map(e=>[idx['m:'+e.mem],idx['e:'+e.entity]]).filter(l=>l[0]!=null&&l[1]!=null);
 if(!nodes.length){cv.getContext('2d').fillStyle='#a895d8';cv.getContext('2d').fillText('No entities yet — save memories that mention CamelCase or `code` terms.',20,40);return;}
 const ctx=cv.getContext('2d');let drag=null;
 cv.onmousedown=e=>{const m=pos(e);drag=near(m);};
 cv.onmouseup=()=>drag=null;
 cv.onmousemove=e=>{const m=pos(e);if(drag){drag.x=m.x;drag.y=m.y;drag.vx=drag.vy=0;}
   const h=near(m);const tip=$('#gtip');if(h){tip.style.display='block';tip.style.left=e.clientX+12+'px';tip.style.top=e.clientY+12+'px';tip.textContent=h.label;}else tip.style.display='none';};
 function pos(e){const r=cv.getBoundingClientRect();return{x:e.clientX-r.left,y:e.clientY-r.top};}
 function near(m){let best=null,bd=1e9;nodes.forEach(n=>{const d=(n.x-m.x)**2+(n.y-m.y)**2;if(d<bd&&d<(n.r+6)**2){bd=d;best=n;}});return best;}
 if(RAF)cancelAnimationFrame(RAF);
 (function step(){const W=cv.width,H=cv.height;
  for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){const a=nodes[i],b=nodes[j];
   let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy+.01,d=Math.sqrt(d2),f=900/d2;
   a.vx+=f*dx/d;a.vy+=f*dy/d;b.vx-=f*dx/d;b.vy-=f*dy/d;}
  links.forEach(l=>{const a=nodes[l[0]],b=nodes[l[1]];let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1,f=(d-72)*.015;
   a.vx+=f*dx/d;a.vy+=f*dy/d;b.vx-=f*dx/d;b.vy-=f*dy/d;});
  nodes.forEach(n=>{n.vx+=(W/2-n.x)*.002;n.vy+=(H/2-n.y)*.002;n.vx*=.86;n.vy*=.86;
   if(n!==drag){n.x+=n.vx;n.y+=n.vy;}n.x=Math.max(n.r,Math.min(W-n.r,n.x));n.y=Math.max(n.r,Math.min(H-n.r,n.y));});
  ctx.clearRect(0,0,W,H);ctx.strokeStyle='#ffffff1f';ctx.lineWidth=1;
  links.forEach(l=>{const a=nodes[l[0]],b=nodes[l[1]];ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();});
  nodes.forEach(n=>{ctx.beginPath();ctx.arc(n.x,n.y,n.r,0,7);
   ctx.fillStyle=n.type=='e'?'#22d3ee':(TC[n.tier]||'#a78bfa');ctx.fill();
   if(n.type=='e'){ctx.fillStyle='#f8fafc';ctx.font='12px sans-serif';ctx.fillText(n.label,n.x+n.r+3,n.y+4);}});
  RAF=requestAnimationFrame(step);})();
}

async function setFoot(){const s=await api('/api/stats');$('#foot').textContent=s.db;}
nav(location.hash.slice(1)||'overview');setFoot();
</script></body></html>"""


def _make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, _PAGE, "text/html; charset=utf-8")
            if u.path == "/api/stats":
                return self._send(200, json.dumps(store.stats()))
            if u.path == "/api/graph":
                return self._send(200, json.dumps(store.graph()))
            if u.path == "/api/lessons":
                return self._send(200, json.dumps({"lessons": store.lessons(limit=100)}))
            if u.path == "/api/sessions":
                return self._send(200, json.dumps({"sessions": store.sessions()}))
            if u.path == "/api/audit":
                return self._send(200, json.dumps({"audit": store.audit_log(limit=100)}))
            if u.path == "/api/memories":
                qs = parse_qs(u.query)
                q = (qs.get("q") or [""])[0]
                tier = (qs.get("tier") or [None])[0]
                return self._send(200, json.dumps(
                    {"memories": store.search(q, limit=200, tier=tier)}))
            return self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            body = self._json_body()
            if path == "/api/memories":
                try:
                    mid = store.save(body.get("title"), body.get("content"),
                                     tier=body.get("tier"), tags=body.get("tags"),
                                     session=body.get("session", ""),
                                     namespace=body.get("namespace") or DEFAULT_NAMESPACE)
                    return self._send(200, json.dumps({"id": mid}))
                except ValueError as e:
                    return self._send(400, json.dumps({"error": str(e)}))
            if path == "/api/forget":
                n = store.forget(mem_id=body.get("id"), query=body.get("query"))
                return self._send(200, json.dumps({"forgotten": n}))
            if path == "/api/pin":
                ok = store.pin(body.get("id"), pinned=body.get("pinned", True))
                return self._send(200, json.dumps({"ok": ok}))
            if path == "/api/learn":
                created = store.learn(min_support=body.get("min_support", 2))
                return self._send(200, json.dumps(
                    {"created": [{"id": i, "label": lbl} for i, lbl in created]}))
            if path == "/api/lesson":
                try:
                    mid = store.add_lesson(body.get("title"), body.get("content"))
                    return self._send(200, json.dumps({"id": mid}))
                except ValueError as e:
                    return self._send(400, json.dumps({"error": str(e)}))
            if path == "/api/consolidate":
                return self._send(200, json.dumps(store.consolidate()))
            return self._send(404, json.dumps({"error": "not found"}))

    return Handler


def make_server(store, host="127.0.0.1", port=DEFAULT_PORT):
    return ThreadingHTTPServer((host, port), _make_handler(store))


_DASHBOARD = {"thread": None, "url": None}


def start_dashboard(store, host="127.0.0.1", port=DEFAULT_PORT) -> str:
    if _DASHBOARD["url"]:
        return _DASHBOARD["url"]
    srv = make_server(store, host, port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _DASHBOARD["thread"] = t
    _DASHBOARD["url"] = f"http://{host}:{srv.server_address[1]}"
    return _DASHBOARD["url"]


def serve_forever(host="127.0.0.1", port=DEFAULT_PORT):
    store = MemoryStore()
    srv = make_server(store, host, port)
    print(f"[memento] memory dashboard → http://{host}:{srv.server_address[1]}")
    srv.serve_forever()
