#!/usr/bin/env python3
"""memento_memory_pg — PostgreSQL backend for shared, per-team memory.

A drop-in alternative to the local SQLite ``MemoryStore`` for **team sharing**:
many people / machines read and write one Postgres database concurrently, scoped
by ``namespace`` (= team). Mirrors the public API of
``memento_memory.MemoryStore`` so the MCP tools and dashboard work unchanged.

Search:
- **BM25-ish full-text** via Postgres ``tsvector`` + ``ts_rank``.
- **Vector** via the same term-frequency embedding cosine as the SQLite engine,
  fused with FTS through Reciprocal Rank Fusion (``mode``: hybrid/bm25/vector).
- **pgvector** path: when a *dense* embedder is supplied and the ``vector``
  extension is present, embeddings are stored in a ``vector`` column and ranked
  with ``<=>`` (ANN). Without one it falls back to the lexical cosine, so it runs
  on plain Postgres too.

Requires ``psycopg`` (v3) via the ``postgres`` extra:
    pip install "devin-memento[postgres]"
Select it by DSN:
    MEMENTO_DB_URL=postgresql://user:pass@host:5432/memento
"""
from __future__ import annotations

import hashlib
import time

from memento_memory import (
    TIERS, DEFAULT_TIER, DEFAULT_NAMESPACE, CAPTURE_EVENTS, RRF_K, VEC_MIN,
    WORKING_TTL_S, PROMOTE_ACCESS, Embedder, cosine, extract_entities,
    redact_secrets, MemoryStore as _SqliteStore,
)


class MemoryStorePG:
    """Postgres-backed memory store. Same public API as MemoryStore."""

    decay_score = staticmethod(_SqliteStore.decay_score)   # reuse the formula

    def __init__(self, dsn, embedder=None, dense_embedder=None):
        import psycopg  # lazy: only needed for the PG backend
        from psycopg.rows import dict_row
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        self.embedder = embedder or Embedder()
        self.dense = dense_embedder          # optional: enables pgvector ANN
        self.pgvector = False
        self.db_path = dsn                    # for stats() parity
        self._init_db()

    def _conn(self):
        # connect-per-op keeps it thread-safe under the threaded dashboard
        return self._psycopg.connect(self.dsn, autocommit=True,
                                     row_factory=self._dict_row)

    def _init_db(self):
        with self._conn() as c:
            if self.dense is not None:
                try:
                    c.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    self.pgvector = True
                except Exception:
                    self.pgvector = False
            c.execute("""
                CREATE TABLE IF NOT EXISTS memories(
                    id text PRIMARY KEY,
                    tier text NOT NULL DEFAULT 'episodic',
                    namespace text NOT NULL DEFAULT 'default',
                    title text NOT NULL,
                    content text NOT NULL,
                    tags text NOT NULL DEFAULT '',
                    session text NOT NULL DEFAULT '',
                    source text NOT NULL DEFAULT 'manual',
                    actor text NOT NULL DEFAULT '',
                    created_ts double precision NOT NULL,
                    accessed_ts double precision NOT NULL,
                    access_count int NOT NULL DEFAULT 0,
                    pinned int NOT NULL DEFAULT 0,
                    search_tsv tsvector GENERATED ALWAYS AS (
                        to_tsvector('english',
                            coalesce(title,'')||' '||coalesce(content,'')||' '||coalesce(tags,''))
                    ) STORED
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS memories_tsv_idx "
                      "ON memories USING gin(search_tsv)")
            c.execute("CREATE INDEX IF NOT EXISTS memories_ns_idx "
                      "ON memories(namespace)")
            c.execute("""CREATE TABLE IF NOT EXISTS mem_entities(
                    mem_id text NOT NULL, entity text NOT NULL,
                    PRIMARY KEY(mem_id, entity))""")
            c.execute("""CREATE TABLE IF NOT EXISTS audit(
                    ts double precision NOT NULL, op text NOT NULL,
                    mem_id text NOT NULL DEFAULT '', actor text NOT NULL DEFAULT '',
                    detail text NOT NULL DEFAULT '')""")
            if self.pgvector:
                c.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS "
                          f"embedding vector({self.dense.dim})")

    # ── audit ───────────────────────────────────────────────────────────────

    def _audit(self, c, op, mem_id="", actor="", detail=""):
        c.execute("INSERT INTO audit(ts,op,mem_id,actor,detail) VALUES(%s,%s,%s,%s,%s)",
                  (time.time(), op, mem_id, actor, detail))

    def audit_log(self, limit=50):
        with self._conn() as c:
            return c.execute("SELECT * FROM audit ORDER BY ts DESC LIMIT %s",
                             (int(limit),)).fetchall()

    # ── writes ──────────────────────────────────────────────────────────────

    @staticmethod
    def _norm_tags(tags):
        if isinstance(tags, (list, tuple)):
            return ",".join(t.strip() for t in tags if str(t).strip())
        return str(tags or "").strip()

    def _dense_literal(self, text):
        v = self.dense.embed(text)
        return "[" + ",".join(str(float(x)) for x in v) + "]"

    def save(self, title, content, tier=None, tags=None, session="",
             source="manual", namespace=DEFAULT_NAMESPACE, actor=""):
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
        with self._conn() as c:
            c.execute("""
                INSERT INTO memories(id,tier,namespace,title,content,tags,session,
                                     source,actor,created_ts,accessed_ts,access_count,pinned)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0)
                ON CONFLICT(id) DO UPDATE SET tier=excluded.tier, tags=excluded.tags,
                    session=excluded.session, accessed_ts=excluded.accessed_ts
            """, (mem_id, tier, namespace, title, content, tags, session, source,
                  actor, now, now))
            if self.pgvector:
                c.execute("UPDATE memories SET embedding=%s::vector WHERE id=%s",
                          (self._dense_literal(title + " " + content), mem_id))
            c.execute("DELETE FROM mem_entities WHERE mem_id=%s", (mem_id,))
            for ent in extract_entities(title + " " + content):
                c.execute("INSERT INTO mem_entities(mem_id,entity) VALUES(%s,%s) "
                          "ON CONFLICT DO NOTHING", (mem_id, ent))
            self._audit(c, "save", mem_id, actor, tier)
        return mem_id

    def capture(self, event_type, payload, session="", namespace=DEFAULT_NAMESPACE,
                actor="agent"):
        et = event_type if event_type in CAPTURE_EVENTS else (event_type or "Event")
        body = payload if isinstance(payload, str) else __import__("json").dumps(payload)
        return self.save(f"[{et}] {body[:60]}", body or et, tier="working",
                         session=session, source="hook", namespace=namespace, actor=actor)

    def pin(self, mem_id, pinned=True):
        with self._conn() as c:
            cur = c.execute("UPDATE memories SET pinned=%s WHERE id=%s",
                            (1 if pinned else 0, mem_id))
            self._audit(c, "pin" if pinned else "unpin", mem_id)
            return cur.rowcount > 0

    def forget(self, mem_id=None, query=None):
        with self._conn() as c:
            if mem_id:
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM memories WHERE id=%s", (mem_id,)).fetchall()]
            elif query:
                ids = [m["id"] for m in self.search(query, limit=1000)]
            else:
                return 0
            if ids:
                c.execute("DELETE FROM memories WHERE id = ANY(%s)", (ids,))
                c.execute("DELETE FROM mem_entities WHERE mem_id = ANY(%s)", (ids,))
                for i in ids:
                    self._audit(c, "forget", i)
        return len(ids)

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, mem_id):
        with self._conn() as c:
            return c.execute("SELECT * FROM memories WHERE id=%s", (mem_id,)).fetchone()

    def list(self, limit=20, tier=None, session=None, namespace=None):
        sql, clauses, params = "SELECT * FROM memories", [], []
        if tier in TIERS:
            clauses.append("tier=%s"); params.append(tier)
        if session:
            clauses.append("session=%s"); params.append(session)
        if namespace:
            clauses.append("namespace=%s"); params.append(namespace)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_ts DESC LIMIT %s"; params.append(int(limit))
        with self._conn() as c:
            return c.execute(sql, params).fetchall()

    def _bm25_ids(self, c, query, tier, namespace):
        extra = (" AND tier=%s" if tier in TIERS else "") + \
                (" AND namespace=%s" if namespace else "")
        tail = ([tier] if tier in TIERS else []) + ([namespace] if namespace else [])
        rows = c.execute(
            "SELECT id FROM memories WHERE search_tsv @@ websearch_to_tsquery('english', %s)"
            + extra + " ORDER BY ts_rank(search_tsv, websearch_to_tsquery('english', %s)) DESC",
            [query] + tail + [query]).fetchall()
        return [r["id"] for r in rows]

    def _vector_ids(self, c, query, tier, namespace):
        extra = (" AND tier=%s" if tier in TIERS else "") + \
                (" AND namespace=%s" if namespace else "")
        tail = ([tier] if tier in TIERS else []) + ([namespace] if namespace else [])
        if self.pgvector:
            rows = c.execute(
                "SELECT id FROM memories WHERE embedding IS NOT NULL" + extra +
                " ORDER BY embedding <=> %s::vector LIMIT 50",
                tail + [self._dense_literal(query)]).fetchall()
            return [r["id"] for r in rows]
        qv = self.embedder.embed(query)
        if not qv:
            return []
        rows = c.execute("SELECT id,title,content FROM memories WHERE TRUE" + extra,
                         tail).fetchall()
        scored = []
        for r in rows:
            sim = cosine(qv, self.embedder.embed(r["title"] + " " + r["content"]))
            if sim >= VEC_MIN:
                scored.append((sim, r["id"]))
        scored.sort(reverse=True)
        return [i for _, i in scored]

    def search(self, query, limit=10, tier=None, namespace=None, mode="hybrid"):
        q = (query or "").strip()
        if not q:
            return self.list(limit=limit, tier=tier, namespace=namespace)
        with self._conn() as c:
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
            if not order:
                return []
            rows = {r["id"]: r for r in c.execute(
                "SELECT * FROM memories WHERE id = ANY(%s)", (order,)).fetchall()}
            now = time.time()
            c.execute("UPDATE memories SET accessed_ts=%s, access_count=access_count+1 "
                      "WHERE id = ANY(%s)", (now, order))
            return [rows[i] for i in order if i in rows]

    def related(self, mem_id, limit=10):
        with self._conn() as c:
            ents = [r["entity"] for r in c.execute(
                "SELECT entity FROM mem_entities WHERE mem_id=%s", (mem_id,)).fetchall()]
            if not ents:
                return []
            return c.execute(
                "SELECT m.*, COUNT(*) AS shared FROM mem_entities e "
                "JOIN memories m ON m.id=e.mem_id "
                "WHERE e.entity = ANY(%s) AND e.mem_id<>%s "
                "GROUP BY m.id ORDER BY shared DESC, m.created_ts DESC LIMIT %s",
                (ents, mem_id, int(limit))).fetchall()

    def graph(self, limit=40, namespace=None):
        nw = " WHERE m.namespace=%s" if namespace else ""
        np = [namespace] if namespace else []
        with self._conn() as c:
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

    def consolidate(self, now=None):
        now = now if now is not None else time.time()
        promoted = forgotten = 0
        with self._conn() as c:
            for m in c.execute("SELECT * FROM memories").fetchall():
                if m["pinned"]:
                    continue
                if (m["tier"] == "working" and m["access_count"] == 0
                        and now - m["created_ts"] > WORKING_TTL_S):
                    c.execute("DELETE FROM memories WHERE id=%s", (m["id"],))
                    c.execute("DELETE FROM mem_entities WHERE mem_id=%s", (m["id"],))
                    self._audit(c, "consolidate-forget", m["id"])
                    forgotten += 1
                elif m["access_count"] >= PROMOTE_ACCESS and m["tier"] in ("working", "episodic"):
                    nxt = "episodic" if m["tier"] == "working" else "semantic"
                    c.execute("UPDATE memories SET tier=%s WHERE id=%s", (nxt, m["id"]))
                    self._audit(c, "consolidate-promote", m["id"], detail=nxt)
                    promoted += 1
        return {"promoted": promoted, "forgotten": forgotten}

    # ── lessons ────────────────────────────────────────────────────────────────

    def patterns(self, min_support=2, namespace=None):
        ns = " AND m.namespace=%s" if namespace else ""
        nsp = [namespace] if namespace else []
        with self._conn() as c:
            ents = [(r["entity"], r["n"]) for r in c.execute(
                "SELECT e.entity, COUNT(*) AS n FROM mem_entities e "
                "JOIN memories m ON m.id=e.mem_id WHERE m.source<>'lesson'" + ns +
                " GROUP BY e.entity HAVING COUNT(*)>=%s ORDER BY n DESC",
                nsp + [min_support]).fetchall()]
            tagc = {}
            for r in c.execute("SELECT tags FROM memories m WHERE source<>'lesson'" + ns,
                               nsp).fetchall():
                for t in (r["tags"] or "").split(","):
                    t = t.strip()
                    if t:
                        tagc[t] = tagc.get(t, 0) + 1
            tags = [(t, n) for t, n in sorted(tagc.items(), key=lambda x: -x[1])
                    if n >= min_support]
            fails = c.execute(
                "SELECT * FROM memories WHERE source='hook' AND "
                "(title ILIKE '%%Failure%%' OR content ILIKE '%%fail%%' "
                "OR content ILIKE '%%error%%')").fetchall()
        return {"entities": ents, "tags": tags, "failures": fails}

    def learn(self, min_support=2, namespace=DEFAULT_NAMESPACE):
        p = self.patterns(min_support, None if namespace == DEFAULT_NAMESPACE else namespace)
        with self._conn() as c:
            old = [r["id"] for r in c.execute(
                "SELECT id FROM memories WHERE source='lesson' AND pinned=0").fetchall()]
            if old:
                c.execute("DELETE FROM memories WHERE id = ANY(%s)", (old,))
                c.execute("DELETE FROM mem_entities WHERE mem_id = ANY(%s)", (old,))
        created = []
        for ent, n in p["entities"][:10]:
            mid = self.save(f"Lesson: recurring focus on {ent}",
                            f"'{ent}' recurs across {n} memories — a stable topic; "
                            f"consider a dedicated SKILL.md section or guardrail for it.",
                            tier="semantic", tags="lesson", source="lesson",
                            namespace=namespace)
            created.append((mid, ent))
        if p["failures"]:
            eg = ", ".join(sorted({f["title"] for f in p["failures"]})[:5])
            mid = self.save("Lesson: recurring failures",
                            f"{len(p['failures'])} failure events captured (e.g. {eg}). "
                            f"Find the root cause and encode a check so it does not recur.",
                            tier="semantic", tags="lesson", source="lesson",
                            namespace=namespace)
            created.append((mid, "failures"))
        return created

    def lessons(self, limit=20, namespace=None):
        nw = " AND namespace=%s" if namespace else ""
        np = [namespace] if namespace else []
        with self._conn() as c:
            return c.execute("SELECT * FROM memories WHERE source='lesson'" + nw +
                             " ORDER BY pinned DESC, created_ts DESC LIMIT %s",
                             np + [int(limit)]).fetchall()

    def add_lesson(self, title, content, namespace=DEFAULT_NAMESPACE):
        mid = self.save(title, content, tier="semantic", tags="lesson",
                        source="lesson", namespace=namespace)
        self.pin(mid, True)
        return mid

    def sessions(self):
        with self._conn() as c:
            return c.execute(
                "SELECT session, COUNT(*) AS n, MAX(created_ts) AS last "
                "FROM memories WHERE session<>'' GROUP BY session ORDER BY last DESC"
            ).fetchall()

    def namespaces(self):
        with self._conn() as c:
            return c.execute("SELECT namespace, COUNT(*) AS n FROM memories "
                             "GROUP BY namespace ORDER BY n DESC").fetchall()

    def stats(self):
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
            by_tier = {r["tier"]: r["n"] for r in c.execute(
                "SELECT tier, COUNT(*) AS n FROM memories GROUP BY tier").fetchall()}
            entities = c.execute(
                "SELECT COUNT(DISTINCT entity) AS n FROM mem_entities").fetchone()["n"]
            namespaces = c.execute(
                "SELECT COUNT(DISTINCT namespace) AS n FROM memories").fetchone()["n"]
        return {"total": total, "by_tier": by_tier, "entities": entities,
                "namespaces": namespaces, "fts": True, "mode": "team",
                "backend": "postgres" + ("+pgvector" if self.pgvector else ""),
                "db": "postgresql"}

    def snapshot(self, path):
        import json
        with self._conn() as c:
            mems = c.execute("SELECT * FROM memories").fetchall()
            ents = c.execute("SELECT * FROM mem_entities").fetchall()
        for m in mems:
            m.pop("search_tsv", None); m.pop("embedding", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "memories": mems, "mem_entities": ents}, f, indent=2)
        return len(mems)

    def restore(self, path):
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        mems = data.get("memories", [])
        with self._conn() as c:
            for m in mems:
                c.execute("""INSERT INTO memories(id,tier,namespace,title,content,tags,
                        session,source,actor,created_ts,accessed_ts,access_count,pinned)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                        content=excluded.content, tier=excluded.tier""",
                    (m.get("id"), m.get("tier", "episodic"), m.get("namespace", "default"),
                     m.get("title", ""), m.get("content", ""), m.get("tags", ""),
                     m.get("session", ""), m.get("source", "restore"), m.get("actor", ""),
                     m.get("created_ts", time.time()), m.get("accessed_ts", time.time()),
                     m.get("access_count", 0), m.get("pinned", 0)))
            for e in data.get("mem_entities", []):
                c.execute("INSERT INTO mem_entities(mem_id,entity) VALUES(%s,%s) "
                          "ON CONFLICT DO NOTHING", (e.get("mem_id"), e.get("entity")))
            self._audit(c, "restore", detail=path)
        return len(mems)
