# Cognition outreach — memento (Devin MCP)

> Send via cognition.com contact/partnerships, Devin in-app support, or @cognition on X.
> Keep it short; partnerships teams skim. Name/role filled in — drop the DEGIRO
> mention if you'd rather not associate your employer; add usage numbers if you have any.

---

**Subject:** Community MCP for Devin — "memento": Devin that improves its own skills overnight

Hi Cognition team,

I'm a software engineer at DEGIRO, where our team uses Devin day to day. As a
personal open-source project, I built **memento**, an MCP server for Devin that
gives it a nightly **self-improvement loop**: it harvests a team's Devin sessions, mines recurring
tasks, proposes bounded edits to a long-term `SKILL.md`, and only adopts a change
when it **strictly beats a held-out validation gate** (built on Microsoft's
SkillOpt). It also ships a built-in memory engine — hybrid BM25 + vector search,
memory tiers, a knowledge graph, auto-derived "lessons," and a local dashboard.

The piece I think is most interesting for Devin teams: **shared team memory**.
It runs in two modes — **solo** (local SQLite, zero config) or **team** (one
shared **Postgres + pgvector**, scoped per team by namespace), switched with a
single env var. In team mode everyone's Devin reads and writes the same memory
and lessons, with a team selector in the dashboard — so hard-won context and
conventions become a **shared, persistent team asset** instead of living in one
person's session history.

It's live and easy to try:
- Repo: https://github.com/xerxes-y/memento
- PyPI: `uvx devin-memento` — https://pypi.org/project/devin-memento/
- One-click bundle: `.mcpb` on the GitHub release
- Today it installs via *Settings → Connections → MCP servers → Add a custom MCP*
  (Command `uvx`, Args `["devin-memento"]`).

I'd love to explore a **Marketplace listing** so Devin teams can find it. One
honest note: it's a **local / STDIO** server today (it reads local Devin
transcripts and runs a local optimizer), which differs from the hosted/OAuth
servers in the gallery. The team mode (shared Postgres backend) is already a step
toward a hosted model, so I'm happy to discuss whether the local form fits the
curated list as-is, or to build a fully hosted variant if that's the bar.

Could we find 15 minutes? I can walk through a live demo.

Thanks,
Khashayar Yadmand
Software Engineer, DEGIRO (Devin user) · github.com/xerxes-y · personal project
