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

DEFAULT_DB = os.path.expanduser(
    os.environ.get("MEMENTO_MEMORY_DB", "~/.memento/memory.db"))
DEFAULT_EXPORT = os.path.expanduser(
    os.environ.get("MEMENTO_MEMORY_PATH", "~/.agentmemory/standalone.json"))
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

    def update(self, mem_id, title=None, content=None, tier=None, tags=None,
               namespace=None, actor="") -> bool:
        """Edit an existing memory in place (id is kept stable even though it was
        originally derived from title+content). Only provided fields change.
        Refreshes the FTS index and extracted entities. Returns False if the id
        does not exist, or (with namespace set) belongs to another team."""
        with self._connect() as c:
            sql = "SELECT * FROM memories WHERE id=?"
            params = [mem_id]
            if namespace:  # team gate: can't edit another team's memory
                sql += " AND namespace=?"; params.append(namespace)
            row = c.execute(sql, params).fetchone()
            if not row:
                return False
            cur = self._row(row)
            new_title = redact_secrets(title.strip()) if title is not None else cur["title"]
            new_content = redact_secrets(content.strip()) if content is not None else cur["content"]
            if not new_title or not new_content:
                raise ValueError("both 'title' and 'content' are required")
            new_tier = tier if tier in TIERS else cur["tier"]
            new_tags = self._norm_tags(tags) if tags is not None else cur["tags"]
            c.execute("UPDATE memories SET title=?, content=?, tier=?, tags=?, "
                      "accessed_ts=? WHERE id=?",
                      (new_title, new_content, new_tier, new_tags, time.time(), mem_id))
            if self.fts:
                c.execute("DELETE FROM memories_fts WHERE id=?", (mem_id,))
                c.execute("INSERT INTO memories_fts(id,title,content,tags) "
                          "VALUES(?,?,?,?)", (mem_id, new_title, new_content, new_tags))
            c.execute("DELETE FROM mem_entities WHERE mem_id=?", (mem_id,))
            for ent in extract_entities(new_title + " " + new_content):
                c.execute("INSERT OR IGNORE INTO mem_entities(mem_id,entity) "
                          "VALUES(?,?)", (mem_id, ent))
            self._audit(c, "edit", mem_id, actor, new_tier)
        self._export()
        return True

    def forget(self, mem_id=None, query=None, namespace=None) -> int:
        with self._connect() as c:
            if mem_id:
                sql = "SELECT id FROM memories WHERE id=?"
                params = [mem_id]
                if namespace:  # team gate: can't delete another team's memory by id
                    sql += " AND namespace=?"; params.append(namespace)
                ids = [r["id"] for r in c.execute(sql, params)]
            elif query:
                ids = [m["id"] for m in self.search(query, limit=1000, namespace=namespace)]
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

    def graph(self, limit=40, namespace=None) -> dict:
        """Entities, the memories they touch, and edges — for the graph viz.
        Scoped to one namespace (team) when given."""
        nw = " WHERE m.namespace=?" if namespace else ""
        np = [namespace] if namespace else []
        with self._connect() as c:
            rows = c.execute(
                "SELECT e.mem_id AS mem_id, e.entity AS entity, m.title AS title, "
                "m.tier AS tier FROM mem_entities e JOIN memories m ON m.id=e.mem_id"
                + nw, np).fetchall()
        counts, mems, all_edges = {}, {}, []
        for r in rows:
            counts[r["entity"]] = counts.get(r["entity"], 0) + 1
            mems[r["mem_id"]] = {"id": r["mem_id"], "title": r["title"], "tier": r["tier"]}
            all_edges.append({"mem": r["mem_id"], "entity": r["entity"]})
        top = sorted(counts.items(), key=lambda x: -x[1])[:int(limit)]
        keep = {n for n, _ in top}
        edges = [e for e in all_edges if e["entity"] in keep]
        used = {e["mem"] for e in edges}
        return {"entities": [{"name": n, "count": c} for n, c in top],
                "memories": [mems[i] for i in used], "edges": edges}

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

    def lessons(self, limit=20, namespace=None) -> list:
        nw = " AND namespace=?" if namespace else ""
        np = [namespace] if namespace else []
        with self._connect() as c:
            return [self._row(r) for r in c.execute(
                "SELECT * FROM memories WHERE source='lesson'" + nw +
                " ORDER BY pinned DESC, created_ts DESC LIMIT ?", np + [int(limit)])]

    def add_lesson(self, title, content, namespace=DEFAULT_NAMESPACE) -> str:
        """Manually author a lesson. Pinned, so memory_learn never wipes it."""
        mid = self.save(title, content, tier="semantic", tags="lesson",
                        source="lesson", namespace=namespace)
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
                "namespaces": namespaces, "fts": self.fts, "db": self.db_path,
                "mode": "solo", "backend": "sqlite"}

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


# ── backend factory ───────────────────────────────────────────────────────────

def open_store(export_path=None, db_path=None):
    """Open the memory store. Shared **Postgres** when ``MEMENTO_DB_URL`` is a
    postgres DSN (team mode), else the local **SQLite** store."""
    dsn = os.environ.get("MEMENTO_DB_URL", "")
    if dsn.startswith(("postgres://", "postgresql://")):
        import memento_memory_pg
        return memento_memory_pg.MemoryStorePG(dsn)
    return MemoryStore(db_path=db_path, export_path=export_path)


# ── local web dashboard (stdlib http.server, vanilla JS) ──────────────────────

_PAGE = r"""<!doctype html><html lang=en data-theme=light><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>memento · team memory</title><style>
*{box-sizing:border-box}
:root{
 --bg:#f4f5fb; --panel:#ffffff; --panel2:#f8f9fd; --border:#e6e8f0;
 --text:#1c2030; --muted:#6b7185; --faint:#9aa0b4;
 --accent:#5b5bf0; --accent-weak:#5b5bf015; --accent-line:#5b5bf040;
 --good:#1aa06d; --warn:#c98a00; --bad:#d6455d; --shadow:0 1px 2px #1c20300d,0 2px 8px #1c203008;
 --working:#6b7185; --episodic:#5b5bf0; --semantic:#1aa06d; --procedural:#c98a00;
}
[data-theme=dark]{
 --bg:#0c0e16; --panel:#141826; --panel2:#1a1f30; --border:#262c40;
 --text:#e7e9f3; --muted:#9aa0b8; --faint:#6b7290;
 --accent:#8a8aff; --accent-weak:#8a8aff1f; --accent-line:#8a8aff50;
 --shadow:0 1px 2px #0006,0 4px 14px #0004;
}
body{margin:0;font:14px/1.55 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
 color:var(--text);background:var(--bg)}
a{color:var(--accent);text-decoration:none}
button{font:inherit;cursor:pointer}

/* shell */
.top{height:56px;display:flex;align-items:center;gap:14px;padding:0 18px;
 background:var(--panel);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:20}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:16px;letter-spacing:.2px}
.brand .mk{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,var(--accent),#9b6bff);
 display:grid;place-items:center;color:#fff;font-size:15px}
.omni{flex:1;max-width:560px;position:relative}
.omni input{width:100%;height:38px;padding:0 12px 0 36px;border:1px solid var(--border);border-radius:10px;
 background:var(--panel2);color:var(--text);outline:none}
.omni input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-weak)}
.omni .ic{position:absolute;left:11px;top:9px;color:var(--faint)}
.omni .kbd{position:absolute;right:9px;top:8px;font-size:11px;color:var(--faint);border:1px solid var(--border);
 border-radius:6px;padding:1px 6px;background:var(--panel)}
.top .sp{flex:1}
.sel{height:38px;border:1px solid var(--border);border-radius:10px;background:var(--panel2);color:var(--text);
 padding:0 10px;outline:none}
.sel:focus{border-color:var(--accent)}
.roleseg{display:flex;background:var(--panel2);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.roleseg button{border:0;background:transparent;color:var(--muted);padding:8px 12px;font-size:13px;display:flex;gap:6px;align-items:center}
.roleseg button.on{background:var(--accent);color:#fff}
.iconbtn{width:38px;height:38px;border:1px solid var(--border);border-radius:10px;background:var(--panel2);
 color:var(--muted);display:grid;place-items:center}
.iconbtn:hover{color:var(--text)}

.wrap{display:flex;min-height:calc(100vh - 56px)}
aside{width:218px;flex:none;padding:16px 12px;border-right:1px solid var(--border);background:var(--panel);
 position:sticky;top:56px;height:calc(100vh - 56px);display:flex;flex-direction:column}
nav button{display:flex;align-items:center;gap:10px;width:100%;text-align:left;margin:2px 0;padding:9px 11px;
 border:0;border-radius:9px;background:transparent;color:var(--muted);font-size:14px}
nav button .ic{width:18px;text-align:center;opacity:.85}
nav button.on{background:var(--accent-weak);color:var(--accent);font-weight:600}
nav button:hover:not(.on){background:var(--panel2);color:var(--text)}
.navlabel{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--faint);margin:14px 8px 4px}
aside .foot{margin-top:auto;font-size:11px;color:var(--faint);padding:10px 8px;border-top:1px solid var(--border);
 line-height:1.7;word-break:break-word}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--good);margin-right:5px}

main{flex:1;padding:22px 26px;max-width:1180px;width:100%}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:6px}
h1{font-size:22px;margin:0;font-weight:700;letter-spacing:-.2px}
.sub{color:var(--muted);margin:2px 0 18px}
.lens{display:flex;gap:9px;align-items:center;background:var(--accent-weak);border:1px solid var(--accent-line);
 color:var(--text);border-radius:10px;padding:9px 13px;margin:0 0 18px;font-size:13px}
.lens b{color:var(--accent)}

/* cards / kpis */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
.kpi{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:15px 16px;box-shadow:var(--shadow)}
.kpi .l{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px}
.kpi .n{font-size:28px;font-weight:700;margin-top:6px;letter-spacing:-.5px}
.kpi .d{font-size:12px;color:var(--faint);margin-top:2px}
.kpi .n .up{color:var(--good);font-size:13px;font-weight:600;margin-left:6px}
.cols{display:grid;grid-template-columns:1.4fr 1fr;gap:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow);margin-bottom:16px}
.card h3{margin:0 0 2px;font-size:15px}
.card .ch{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.card .ch .sub{margin:0}

/* table */
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--faint);
 font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border);position:sticky;top:56px;background:var(--panel)}
td{padding:10px;border-bottom:1px solid var(--border);vertical-align:top}
tr:hover td{background:var(--panel2)}
.ttl{font-weight:600}
.snip{color:var(--muted);font-size:12.5px;max-width:520px}
.rowact{display:flex;gap:6px;opacity:0;transition:.12s}
tr:hover .rowact{opacity:1}

/* bits */
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;
 background:var(--panel2);border:1px solid var(--border);white-space:nowrap}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--tc,var(--muted))}
.tag{font-size:11px;color:var(--accent);background:var(--accent-weak);border-radius:6px;padding:1px 7px;margin-right:4px}
.btn{height:36px;padding:0 14px;border:1px solid var(--accent);border-radius:9px;background:var(--accent);color:#fff;
 font-weight:600;font-size:13px;display:inline-flex;align-items:center;gap:7px}
.btn:hover{filter:brightness(1.06)}
.btn.ghost{background:var(--panel);color:var(--text);border-color:var(--border)}
.btn.ghost:hover{background:var(--panel2)}
.btn.danger{background:var(--panel);color:var(--bad);border-color:var(--border)}
.btn.danger:hover{background:#d6455d12}
.btn.sm{height:30px;padding:0 10px;font-size:12px}
.actbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.chip{height:32px;padding:0 12px;border:1px solid var(--border);border-radius:999px;background:var(--panel);
 color:var(--muted);font-size:13px;display:inline-flex;align-items:center;gap:6px}
.chip.on{border-color:var(--accent);color:var(--accent);background:var(--accent-weak)}
.empty{text-align:center;color:var(--muted);padding:48px 0}
.empty .big{font-size:34px;margin-bottom:8px;opacity:.6}
.muted{color:var(--muted)}
.skel{height:14px;border-radius:6px;background:linear-gradient(90deg,var(--panel2),var(--border),var(--panel2));
 background-size:200% 100%;animation:sh 1.2s infinite}
@keyframes sh{to{background-position:-200% 0}}

/* lessons / list */
.litem{padding:13px 0;border-bottom:1px solid var(--border)}
.litem:last-child{border:0}
.litem .lt{font-weight:600}
.litem .lc{color:var(--muted);margin-top:3px}
.pin{color:var(--warn)}

/* modal */
.ov{position:fixed;inset:0;background:#0c0e1666;backdrop-filter:blur(3px);display:none;place-items:center;z-index:40;padding:20px}
.ov.show{display:grid}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:16px;width:100%;max-width:620px;
 max-height:86vh;overflow:auto;box-shadow:0 20px 60px #0004}
.modal .mh{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--panel)}
.modal .mh h3{margin:0;font-size:16px}
.modal .mb{padding:18px 20px}
.modal .mf{padding:14px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:10px;
 position:sticky;bottom:0;background:var(--panel)}
label.fld{display:block;margin-bottom:13px}
label.fld span{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;font-weight:600}
.fld input,.fld textarea,.fld select{width:100%;padding:9px 11px;border:1px solid var(--border);border-radius:9px;
 background:var(--panel2);color:var(--text);outline:none;font:inherit}
.fld textarea{min-height:120px;resize:vertical}
.fld input:focus,.fld textarea:focus,.fld select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-weak)}
.prov{display:grid;grid-template-columns:auto 1fr;gap:7px 16px;font-size:13px;margin:4px 0 6px}
.prov .k{color:var(--faint)}
.x{border:0;background:transparent;color:var(--muted);font-size:20px;line-height:1}

/* toast */
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);background:var(--text);
 color:var(--bg);padding:11px 18px;border-radius:10px;font-size:13.5px;font-weight:500;opacity:0;pointer-events:none;
 transition:.25s;z-index:60;box-shadow:0 8px 30px #0003}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* charts */
.bars{display:flex;flex-direction:column;gap:9px}
.bar{display:grid;grid-template-columns:96px 1fr 40px;align-items:center;gap:10px;font-size:13px}
.bar .track{height:9px;border-radius:6px;background:var(--panel2);overflow:hidden}
.bar .fill{height:100%;border-radius:6px}
svg.spark{display:block;width:100%;height:54px}

@media(max-width:860px){.kpis{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}
 aside{position:fixed;left:-240px;transition:.2s;z-index:30}aside.open{left:0}.omni .kbd{display:none}}
#gcanvas{width:100%;height:460px;border:1px solid var(--border);border-radius:14px;background:var(--panel)}
#gtip{position:fixed;pointer-events:none;background:var(--text);color:var(--bg);border-radius:8px;padding:4px 9px;
 font-size:12px;display:none;max-width:280px;z-index:50}
</style></head><body>

<div class=top>
 <div class=brand><span class=mk>◐</span> memento</div>
 <div class=omni>
  <span class=ic>⌕</span>
  <input id=q placeholder="Search the team's memory…" autocomplete=off>
  <span class=kbd>/</span>
 </div>
 <div class=sp></div>
 <select class=sel id=ns title="Team / namespace"></select>
 <div class=roleseg id=roleseg>
  <button data-r=product>◔ Product</button>
  <button data-r=tester>✓ Tester</button>
  <button data-r=developer>⟨⟩ Dev</button>
 </div>
 <button class=iconbtn id=theme title="Toggle theme">☾</button>
</div>

<div class=wrap>
 <aside id=side>
  <div class=navlabel>Workspace</div>
  <nav id=nav></nav>
  <div class=foot id=foot><span class=dot></span>connecting…</div>
 </aside>
 <main id=main></main>
</div>

<div class=ov id=ov><div class=modal id=modal></div></div>
<div id=toast></div>
<div id=gtip></div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const api=async p=>(await fetch(p)).json();
const post=async(p,b)=>(await fetch(p,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b||{})})).json();
let TT=null; function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');clearTimeout(TT);TT=setTimeout(()=>t.classList.remove('show'),1900);}
const TC={working:'var(--working)',episodic:'var(--episodic)',semantic:'var(--semantic)',procedural:'var(--procedural)'};
const TIERS=['working','episodic','semantic','procedural'];
function badge(t){return `<span class=badge style="--tc:${TC[t]||'var(--muted)'}">${esc(t)}</span>`;}
function fmt(ts){if(!ts)return '—';const d=new Date(ts*1000),n=Date.now()-d,h=36e5;
 if(n<6e4)return 'just now';if(n<h)return Math.floor(n/6e4)+'m ago';if(n<24*h)return Math.floor(n/h)+'h ago';
 if(n<7*24*h)return Math.floor(n/(24*h))+'d ago';return d.toLocaleDateString();}
function tags(s){return (s||'').split(',').filter(Boolean).map(t=>`<span class=tag>${esc(t)}</span>`).join('');}

// ── roles ─────────────────────────────────────────────────────────────────
const ROLES={
 product:{name:'Product Owner',
  lens:'You are seeing the <b>value lens</b> — what the team\'s agent has learned and how its shared knowledge is growing. Plain language, no internals.',
  nav:['dashboard','lessons','memories','sessions'], edit:false, del:false, admin:false},
 tester:{name:'Tester / QA',
  lens:'You are seeing the <b>verification lens</b> — search what the agent knows, inspect where each memory came from, and correct anything that\'s wrong.',
  nav:['dashboard','memories','lessons','sessions','activity'], edit:true, del:true, admin:false},
 developer:{name:'Developer',
  lens:'You are seeing the <b>engineering lens</b> — every field, the knowledge graph, the audit trail, and full read/write control.',
  nav:['dashboard','memories','lessons','graph','sessions','activity'], edit:true, del:true, admin:true},
};
const NAVMETA={dashboard:['◧','Dashboard'],memories:['▤','Memories'],lessons:['✦','Lessons'],
 graph:['❖','Knowledge graph'],sessions:['◷','Sessions'],activity:['≡','Activity']};

let ST={ns:'', role:localStorage.getItem('memento.role')||'developer', view:'dashboard'};
function R(){return ROLES[ST.role];}
function nsq(p){return ST.ns?p+(p.includes('?')?'&':'?')+'ns='+encodeURIComponent(ST.ns):p;}

function applyRole(){
 $$('#roleseg button').forEach(b=>b.classList.toggle('on',b.dataset.r===ST.role));
 const nav=$('#nav');nav.innerHTML=R().nav.map(v=>{const[ic,lb]=NAVMETA[v];
  return `<button data-v="${v}" class="${v===ST.view?'on':''}"><span class=ic>${ic}</span>${lb}</button>`;}).join('');
 $$('#nav button').forEach(b=>b.onclick=()=>go(b.dataset.v));
 if(!R().nav.includes(ST.view))ST.view=R().nav[0];
}
function setRole(r){ST.role=r;localStorage.setItem('memento.role',r);applyRole();go(ST.view);}

function go(v){ST.view=v;location.hash=v;applyRole();
 ({dashboard:vDash,memories:vMem,lessons:vLessons,graph:vGraph,sessions:vSessions,activity:vActivity}[v]||vDash)();}

// ── namespace selector ──────────────────────────────────────────────────────
async function loadNS(){const d=await api('/api/namespaces');const sel=$('#ns');
 const list=d.namespaces&&d.namespaces.length?d.namespaces:['default'];
 sel.innerHTML='<option value="">All teams</option>'+list.map(n=>`<option>${esc(n)}</option>`).join('');
 sel.onchange=()=>{ST.ns=sel.value;go(ST.view);};}

// ── charts ──────────────────────────────────────────────────────────────────
function bars(map){const tot=Object.values(map).reduce((a,b)=>a+b,0)||1;
 return '<div class=bars>'+TIERS.filter(t=>map[t]).map(t=>{const v=map[t]||0;
  return `<div class=bar><div>${badge(t)}</div><div class=track><div class=fill style="width:${v/tot*100}%;background:${TC[t]}"></div></div><div style="text-align:right;color:var(--muted)">${v}</div></div>`;}).join('')+'</div>';}
function spark(pts){if(!pts.length)return '<div class=muted style="padding:14px 0">No recent activity.</div>';
 const max=Math.max(...pts,1),n=pts.length,W=100,H=46;
 const dx=n>1?W/(n-1):0;const d=pts.map((v,i)=>`${i*dx},${H-v/max*H}`).join(' ');
 return `<svg class=spark viewBox="0 0 ${W} ${H}" preserveAspectRatio=none>
  <polyline points="${d}" fill=none stroke="var(--accent)" stroke-width=2 vector-effect=non-scaling-stroke/>
  <polyline points="0,${H} ${d} ${W},${H}" fill="var(--accent-weak)" stroke=none/></svg>`;}
function dayBuckets(audit,days=14){const now=Date.now(),b=Array(days).fill(0);
 audit.forEach(a=>{const age=Math.floor((now-a.ts*1000)/864e5);if(age>=0&&age<days)b[days-1-age]++;});return b;}

// ── dashboard (role-aware) ──────────────────────────────────────────────────
async function vDash(){
 $('#main').innerHTML=`<div class=head><div><h1>Dashboard</h1>
  <p class=sub>${ST.ns?('Team · '+esc(ST.ns)):'All teams'}</p></div></div>
  <div class=lens>${R().lens}</div><div id=body><div class=kpis>
  ${'<div class=kpi><div class="skel" style="width:60%"></div><div class="skel n" style="height:28px;margin-top:8px"></div></div>'.repeat(4)}</div></div>`;
 const [s,au,les]=await Promise.all([api('/api/stats'),api('/api/audit'),api(nsq('/api/lessons'))]);
 const audit=au.audit||[],lessons=les.lessons||[];
 const week=audit.filter(a=>a.op==='save'&&Date.now()-a.ts*1000<7*864e5).length;
 const learned=lessons.length;
 const k=(l,n,d,ic)=>`<div class=kpi><div class=l>${ic||''} ${l}</div><div class=n>${n}</div><div class=d>${d}</div></div>`;
 let kpis;
 if(ST.role==='product'){
  kpis=k('Knowledge items',s.total,'things the agent remembers','◳')
      +k('Lessons learned',learned,'reusable insights','✦')
      +k('Teams',s.namespaces,'sharing this memory','◍')
      +k('Added this week',`${week}<span class=up>▲</span>`,'new memories','＋');
 }else{
  kpis=k('Total memories',s.total,(s.fts?'full-text + vector':'LIKE fallback')+' search','▤')
      +k('Entities',s.entities,'in the knowledge graph','❖')
      +k('Namespaces',s.namespaces,'teams / scopes','◍')
      +k('Saved · 7d',`${week}<span class=up>▲</span>`,'write activity','＋');
 }
 const tierTitle=ST.role==='product'?'What kind of knowledge':'Memory by tier';
 const lessTitle=ST.role==='product'?'What the agent has learned':'Recent lessons';
 $('#body').innerHTML=`<div class=kpis>${kpis}</div>
  <div class=cols>
   <div><div class=card><div class=ch><div><h3>${lessTitle}</h3><p class=sub>${ST.role==='product'?'insights distilled from how the team works':'derived from recurring patterns'}</p></div>
     ${ST.view==='dashboard'&&R().admin?'<button class="btn ghost sm" onclick="doLearn()">✦ Derive</button>':''}</div>
     ${lessons.length?lessons.slice(0,6).map(l=>`<div class=litem><div class=lt>${l.pinned?'<span class=pin>★</span> ':''}${esc(l.title)}</div><div class=lc>${esc((l.content||'').slice(0,160))}</div></div>`).join(''):'<div class=empty><div class=big>✦</div>No lessons yet.</div>'}
    </div></div>
   <div>
    <div class=card><div class=ch><div><h3>${tierTitle}</h3></div></div>${bars(s.by_tier||{})}</div>
    <div class=card><div class=ch><div><h3>Activity</h3><p class=sub>last 14 days</p></div></div>${spark(dayBuckets(audit))}</div>
   </div>
  </div>`;
}

// ── memories: search + table + inspect/edit ─────────────────────────────────
let MODE='hybrid';
async function vMem(){
 const canNew=R().edit;
 $('#main').innerHTML=`<div class=head><div><h1>Memories</h1>
   <p class=sub>${ST.role==='tester'?'search, verify provenance, and correct what the agent knows':'hybrid search · BM25 + semantic vector'}</p></div>
   ${canNew?'<button class=btn onclick="openEdit(null)">＋ New memory</button>':''}</div>
  <div class=lens>${R().lens}</div>
  <div class=filters>
   <input id=mq class=sel style="flex:1;min-width:200px" placeholder="Filter within results…">
   <div class=chip-row id=tierchips></div>
   <button class="chip ${MODE==='hybrid'?'on':''}" data-m=hybrid>Hybrid</button>
   <button class="chip ${MODE==='bm25'?'on':''}" data-m=bm25>Keyword</button>
   <button class="chip ${MODE==='vector'?'on':''}" data-m=vector>Semantic</button>
  </div>
  <div class=card style="padding:0"><table><thead><tr><th>Memory</th><th style=width:110px>Tier</th><th style=width:120px>Updated</th><th style=width:130px></th></tr></thead><tbody id=rows></tbody></table></div>`;
 $('#tierchips').innerHTML='<button class="chip tf on" data-t="">All</button>'+TIERS.map(t=>`<button class="chip tf" data-t="${t}">${t}</button>`).join('');
 $$('#tierchips .tf').forEach(b=>b.onclick=()=>{$$('#tierchips .tf').forEach(x=>x.classList.remove('on'));b.classList.add('on');TIER=b.dataset.t;loadMem();});
 $$('.filters .chip[data-m]').forEach(b=>b.onclick=()=>{MODE=b.dataset.m;$$('.filters .chip[data-m]').forEach(x=>x.classList.toggle('on',x===b));loadMem();});
 $('#mq').oninput=()=>{clearTimeout(window._mt);window._mt=setTimeout(loadMem,180);};
 loadMem();
}
let TIER='';
async function loadMem(){
 const rows=$('#rows');if(rows)rows.innerHTML=`<tr><td colspan=4><div class=skel style="width:40%"></div></td></tr>`;
 const q=$('#q').value||'';
 const d=await api(nsq('/api/memories?mode='+MODE+'&q='+encodeURIComponent(q)+'&tier='+encodeURIComponent(TIER)));
 let mem=d.memories||[];
 const f=($('#mq')&&$('#mq').value||'').toLowerCase();
 if(f)mem=mem.filter(m=>(m.title+' '+m.content+' '+(m.tags||'')).toLowerCase().includes(f));
 if(!mem.length){rows.innerHTML=`<tr><td colspan=4><div class=empty><div class=big>▤</div>No memories match.${R().edit?' <a href="#" onclick="openEdit(null);return false">Add one</a>.':''}</div></td></tr>`;return;}
 rows.innerHTML=mem.map(m=>`<tr>
  <td><div class=ttl>${m.pinned?'<span class=pin>★</span> ':''}<a href="#" onclick="inspect('${m.id}');return false">${esc(m.title)}</a></div>
   <div class=snip>${esc((m.content||'').slice(0,150))}</div><div style=margin-top:6px>${tags(m.tags)}</div></td>
  <td>${badge(m.tier)}</td><td class=muted>${fmt(m.accessed_ts||m.created_ts)}</td>
  <td><div class=rowact>
   <button class="btn ghost sm" onclick="inspect('${m.id}')">Inspect</button>
   ${R().edit?`<button class="btn ghost sm" onclick="openEditId('${m.id}')">Edit</button>`:''}
   ${R().del?`<button class="btn danger sm" onclick="doForget('${m.id}')">✕</button>`:''}
  </div></td></tr>`).join('');
}

async function inspect(id){
 const d=await api('/api/memory?id='+encodeURIComponent(id));if(d.error){toast('Not found');return;}
 const m=d.memory,rel=m.related||[];
 modal(`<div class=mh><h3>${esc(m.title)}</h3><button class=x onclick=closeModal()>×</button></div>
  <div class=mb>
   <div style="margin-bottom:14px">${badge(m.tier)} ${tags(m.tags)}</div>
   <p style="white-space:pre-wrap;margin:0 0 16px">${esc(m.content)}</p>
   <h3 style="font-size:13px;margin:0 0 6px;color:var(--muted)">Provenance</h3>
   <div class=prov>
    <span class=k>Source</span><span>${esc(m.source||'—')}</span>
    <span class=k>Saved by</span><span>${esc(m.actor||'—')}</span>
    <span class=k>Session</span><span>${esc(m.session||'—')}</span>
    <span class=k>Team</span><span>${esc(m.namespace||'default')}</span>
    <span class=k>Created</span><span>${fmt(m.created_ts)}</span>
    <span class=k>Recalled</span><span>${m.access_count||0}× · last ${fmt(m.accessed_ts)}</span>
    <span class=k>ID</span><span class=muted style=font-family:monospace>${esc(m.id)}</span>
   </div>
   ${rel.length?`<h3 style="font-size:13px;margin:16px 0 6px;color:var(--muted)">Related</h3>`+rel.map(r=>`<div class=litem style="padding:8px 0"><a href="#" onclick="inspect('${r.id}');return false">${esc(r.title)}</a> ${badge(r.tier)}</div>`).join(''):''}
  </div>
  <div class=mf>
   ${R().del?`<button class="btn danger" onclick="doForget('${m.id}',true)">Delete</button>`:''}
   ${R().edit?`<button class="btn ghost" onclick='openEdit(${JSON.stringify(m).replace(/'/g,"&#39;")})'>Edit</button>`:''}
   <button class=btn onclick=closeModal()>Close</button>
  </div>`);
}

async function openEditId(id){const d=await api('/api/memory?id='+encodeURIComponent(id));if(d.memory)openEdit(d.memory);}
function openEdit(m){
 const isNew=!m;m=m||{tier:'episodic'};
 modal(`<div class=mh><h3>${isNew?'New memory':'Edit memory'}</h3><button class=x onclick=closeModal()>×</button></div>
  <div class=mb>
   <label class=fld><span>Title</span><input id= et value="${esc(m.title||'')}" placeholder="Short, recognizable summary"></label>
   <label class=fld><span>Content</span><textarea id=ec placeholder="What should the agent remember?">${esc(m.content||'')}</textarea></label>
   <label class=fld><span>Tier</span><select id=etier>${TIERS.map(t=>`<option ${m.tier===t?'selected':''}>${t}</option>`).join('')}</select></label>
   <label class=fld><span>Tags <span class=muted>(comma-separated)</span></span><input id=etags value="${esc(m.tags||'')}" placeholder="build, auth, java"></label>
  </div>
  <div class=mf><button class="btn ghost" onclick=closeModal()>Cancel</button>
   <button class=btn onclick='saveEdit(${isNew?'null':JSON.stringify(m.id)})'>${isNew?'Save memory':'Save changes'}</button></div>`);
 setTimeout(()=>$('#et')&&$('#et').focus(),40);
}
async function saveEdit(id){
 const body={title:$('#et').value,content:$('#ec').value,tier:$('#etier').value,tags:$('#etags').value};
 if(!body.title.trim()||!body.content.trim()){toast('Title and content are required');return;}
 let r;
 if(id){r=await post('/api/update',{id,...body});}
 else{body.namespace=ST.ns||'default';r=await post('/api/memories',body);}
 if(r.error){toast(r.error);return;}
 closeModal();toast(id?'Memory updated':'Memory saved');loadMem&&$('#rows')&&loadMem();
}
async function doForget(id,fromModal){if(!confirm('Delete this memory for the whole team?'))return;
 await post('/api/forget',{id});if(fromModal)closeModal();toast('Deleted');$('#rows')?loadMem():go(ST.view);}

// ── lessons ─────────────────────────────────────────────────────────────────
async function vLessons(){
 $('#main').innerHTML=`<div class=head><div><h1>Lessons</h1>
  <p class=sub>${ST.role==='product'?'plain-language insights the team\'s agent has distilled':'insights derived from recurring patterns — or pin your own'}</p></div>
  ${R().admin?'<div class=actbar style=margin:0><button class="btn ghost" onclick="doLearn()">✦ Derive lessons</button><button class=btn onclick="openLesson()">＋ Add lesson</button></div>':''}</div>
  <div class=lens>${R().lens}</div><div class=card id=lwrap><div class=skel style=width:50%></div></div>`;
 const d=await api(nsq('/api/lessons'));const L=d.lessons||[];
 $('#lwrap').innerHTML=L.length?L.map(l=>`<div class=litem><div class=lt>${l.pinned?'<span class=pin>★</span> ':''}${esc(l.title)}</div><div class=lc>${esc(l.content||'')}</div></div>`).join(''):'<div class=empty><div class=big>✦</div>No lessons yet'+(R().admin?'. <a href="#" onclick="doLearn();return false">Derive some</a> from recurring patterns.':'.')+'</div>';
}
function openLesson(){modal(`<div class=mh><h3>Add lesson</h3><button class=x onclick=closeModal()>×</button></div>
  <div class=mb><label class=fld><span>Title</span><input id=lt></label>
   <label class=fld><span>Lesson</span><textarea id=lc></textarea></label></div>
  <div class=mf><button class="btn ghost" onclick=closeModal()>Cancel</button><button class=btn onclick=saveLesson()>Save</button></div>`);}
async function saveLesson(){const r=await post('/api/lesson',{title:$('#lt').value,content:$('#lc').value,namespace:ST.ns||'default'});
 if(r.error){toast(r.error);return;}closeModal();toast('Lesson added');vLessons();}
async function doLearn(){toast('Deriving…');const r=await post('/api/learn',{});toast('Derived '+((r.created||[]).length)+' lesson(s)');go(ST.view);}

// ── sessions / activity ─────────────────────────────────────────────────────
async function vSessions(){
 $('#main').innerHTML=`<div class=head><div><h1>Sessions</h1><p class=sub>memories grouped by working session</p></div></div>
  <div class=card style=padding:0><table><thead><tr><th>Session</th><th style=width:120px>Memories</th><th style=width:160px>Last activity</th></tr></thead><tbody id=sb></tbody></table></div>`;
 const d=await api('/api/sessions');const S=d.sessions||[];
 $('#sb').innerHTML=S.length?S.map(s=>`<tr><td class=ttl>${esc(s.session)}</td><td>${s.n}</td><td class=muted>${fmt(s.last)}</td></tr>`).join(''):'<tr><td colspan=3><div class=empty><div class=big>◷</div>No sessions recorded.</div></td></tr>';
}
async function vActivity(){
 $('#main').innerHTML=`<div class=head><div><h1>Activity</h1><p class=sub>audit trail — every write to team memory</p></div></div>
  <div class=card style=padding:0><table><thead><tr><th style=width:120px>When</th><th style=width:90px>Op</th><th>Memory</th><th style=width:130px>By</th></tr></thead><tbody id=ab></tbody></table></div>`;
 const d=await api('/api/audit');const A=d.audit||[];
 const opc={save:'var(--good)',edit:'var(--warn)',forget:'var(--bad)',pin:'var(--accent)',unpin:'var(--muted)'};
 $('#ab').innerHTML=A.length?A.map(a=>`<tr><td class=muted>${fmt(a.ts)}</td>
  <td><span class=badge style="--tc:${opc[a.op]||'var(--muted)'}">${esc(a.op)}</span></td>
  <td class=muted style=font-family:monospace>${esc(a.mem_id||'—')}${a.detail?' · '+esc(a.detail):''}</td>
  <td class=muted>${esc(a.actor||'—')}</td></tr>`).join(''):'<tr><td colspan=4><div class=empty><div class=big>≡</div>No activity yet.</div></td></tr>';
}

// ── knowledge graph ─────────────────────────────────────────────────────────
let RAF;
async function vGraph(){
 $('#main').innerHTML=`<div class=head><div><h1>Knowledge graph</h1>
  <p class=sub>entities ◯ and the memories ● that mention them · drag to explore</p></div></div>
  <canvas id=gcanvas></canvas>`;
 if(RAF)cancelAnimationFrame(RAF);
 const g=await api(nsq('/api/graph'));const cv=$('#gcanvas'),ctx=cv.getContext('2d');
 const dpr=devicePixelRatio||1;function size(){cv.width=cv.clientWidth*dpr;cv.height=cv.clientHeight*dpr;}size();
 const cs=getComputedStyle(document.body),AC=cs.getPropertyValue('--accent'),TXc=cs.getPropertyValue('--text'),MU=cs.getPropertyValue('--muted');
 const ents=(g.entities||[]).map(e=>({id:'ent:'+e.name,label:e.name,type:'e',count:e.count}));
 const mns=(g.memories||[]).map(m=>({id:m.id,label:m.title,type:'m'}));
 const nodes=ents.concat(mns).map(n=>({...n,x:Math.random()*cv.width,y:Math.random()*cv.height,vx:0,vy:0,
   r:n.type==='e'?Math.min(12,5+(n.count||1)):7}));
 const idx={};nodes.forEach((n,i)=>idx[n.id]=i);
 const edges=(g.edges||[]).map(e=>({s:e.mem,t:'ent:'+e.entity})).filter(e=>idx[e.s]!=null&&idx[e.t]!=null);
 if(!nodes.length){$('#gcanvas').outerHTML='<div class=empty><div class=big>❖</div>No linked entities yet — add a few memories that share names.</div>';return;}
 let drag=null,tip=$('#gtip');
 function pos(e){const r=cv.getBoundingClientRect();return{x:(e.clientX-r.left)*dpr,y:(e.clientY-r.top)*dpr};}
 function near(m){let b=null,bd=1e9;nodes.forEach(n=>{const d=(n.x-m.x)**2+(n.y-m.y)**2;if(d<bd&&d<(n.r*dpr+8*dpr)**2){bd=d;b=n;}});return b;}
 cv.onmousedown=e=>{drag=near(pos(e));};
 cv.onmousemove=e=>{const m=pos(e);if(drag){drag.x=m.x;drag.y=m.y;drag.vx=drag.vy=0;}
  const h=near(m);if(h){tip.style.display='block';tip.style.left=e.clientX+12+'px';tip.style.top=e.clientY+12+'px';tip.textContent=h.label;}else tip.style.display='none';};
 window.onmouseup=()=>drag=null;
 (function step(){const W=cv.width,H=cv.height;
  for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){const a=nodes[i],b=nodes[j];
   let dx=a.x-b.x,dy=a.y-b.y,d=Math.hypot(dx,dy)||1,f=(2200*dpr)/(d*d);dx/=d;dy/=d;a.vx+=dx*f;a.vy+=dy*f;b.vx-=dx*f;b.vy-=dy*f;}
  edges.forEach(e=>{const a=nodes[idx[e.s]],b=nodes[idx[e.t]];let dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,f=(d-70*dpr)*0.01;dx/=d;dy/=d;a.vx+=dx*f;a.vy+=dy*f;b.vx-=dx*f;b.vy-=dy*f;});
  nodes.forEach(n=>{if(n===drag)return;n.vx+=(W/2-n.x)*0.0008;n.vy+=(H/2-n.y)*0.0008;n.x+=n.vx*=0.86;n.y+=n.vy*=0.86;
   n.x=Math.max(n.r*dpr,Math.min(W-n.r*dpr,n.x));n.y=Math.max(n.r*dpr,Math.min(H-n.r*dpr,n.y));});
  ctx.clearRect(0,0,W,H);ctx.strokeStyle=MU+'55';ctx.lineWidth=dpr;
  edges.forEach(e=>{const a=nodes[idx[e.s]],b=nodes[idx[e.t]];ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();});
  nodes.forEach(n=>{ctx.beginPath();ctx.arc(n.x,n.y,n.r*dpr,0,7);
   ctx.fillStyle=n.type==='e'?MU:AC;ctx.fill();
   if(n.type==='e'){ctx.fillStyle=TXc;ctx.font=12*dpr+'px sans-serif';ctx.fillText(n.label,n.x+n.r*dpr+3,n.y+4*dpr);}});
  RAF=requestAnimationFrame(step);})();
}

// ── footer / boot ───────────────────────────────────────────────────────────
async function setFoot(){const s=await api('/api/stats');
 $('#foot').innerHTML=`<span class=dot></span>${esc(s.mode||'solo')} · ${esc(s.backend||'sqlite')}<br><span class=muted>${esc((s.db||'').split('/').pop())}</span>`;}

$('#roleseg').onclick=e=>{const b=e.target.closest('button');if(b)setRole(b.dataset.r);};
$('#theme').onclick=()=>{const d=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
 document.documentElement.setAttribute('data-theme',d);localStorage.setItem('memento.theme',d);
 $('#theme').textContent=d==='dark'?'☀':'☾';};
function modal(html){$('#modal').innerHTML=html;$('#ov').classList.add('show');}
function closeModal(){$('#ov').classList.remove('show');}
$('#ov').onclick=e=>{if(e.target.id==='ov')closeModal();};
let QT;$('#q').oninput=()=>{clearTimeout(QT);QT=setTimeout(()=>{if(ST.view==='memories')loadMem();else go('memories');},200);};
document.onkeydown=e=>{if(e.key==='/'&&document.activeElement.tagName!=='INPUT'&&document.activeElement.tagName!=='TEXTAREA'){e.preventDefault();$('#q').focus();}
 if(e.key==='Escape')closeModal();};

(function init(){
 const qp=new URLSearchParams(location.search);
 const th=qp.get('theme')||localStorage.getItem('memento.theme')||'light';
 document.documentElement.setAttribute('data-theme',th);$('#theme').textContent=th==='dark'?'☀':'☾';
 const qr=qp.get('role');if(qr&&ROLES[qr]){ST.role=qr;localStorage.setItem('memento.role',qr);}
 if(!ROLES[ST.role])ST.role='developer';
 const h=location.hash.slice(1);applyRole();loadNS().then(()=>{setFoot();go(NAVMETA[h]?h:R().nav[0]);});
})();
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
            qs = parse_qs(u.query)
            ns = (qs.get("ns") or [None])[0] or None
            if u.path in ("/", "/index.html"):
                return self._send(200, _PAGE, "text/html; charset=utf-8")
            if u.path == "/api/stats":
                return self._send(200, json.dumps(store.stats()))
            if u.path == "/api/namespaces":
                return self._send(200, json.dumps({"namespaces": store.namespaces()}))
            if u.path == "/api/graph":
                return self._send(200, json.dumps(store.graph(namespace=ns)))
            if u.path == "/api/lessons":
                return self._send(200, json.dumps(
                    {"lessons": store.lessons(limit=100, namespace=ns)}))
            if u.path == "/api/sessions":
                return self._send(200, json.dumps({"sessions": store.sessions()}))
            if u.path == "/api/audit":
                return self._send(200, json.dumps({"audit": store.audit_log(limit=100)}))
            if u.path == "/api/memories":
                q = (qs.get("q") or [""])[0]
                tier = (qs.get("tier") or [None])[0]
                mode = (qs.get("mode") or ["hybrid"])[0]
                return self._send(200, json.dumps(
                    {"memories": store.search(q, limit=200, tier=tier,
                                              namespace=ns, mode=mode)}))
            if u.path == "/api/memory":
                mem = store.get((qs.get("id") or [""])[0])
                if not mem:
                    return self._send(404, json.dumps({"error": "not found"}))
                mem["related"] = store.related(mem["id"], limit=8)
                return self._send(200, json.dumps({"memory": mem}))
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
            if path == "/api/update":
                try:
                    ok = store.update(body.get("id"), title=body.get("title"),
                                      content=body.get("content"), tier=body.get("tier"),
                                      tags=body.get("tags"), actor=body.get("actor", ""))
                    return self._send(200, json.dumps({"ok": ok}))
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
                    mid = store.add_lesson(body.get("title"), body.get("content"),
                                           namespace=body.get("namespace") or "default")
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
    store = open_store()
    srv = make_server(store, host, port)
    print(f"[memento] memory dashboard → http://{host}:{srv.server_address[1]}")
    srv.serve_forever()
