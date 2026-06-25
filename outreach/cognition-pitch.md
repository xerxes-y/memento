# Cognition outreach — memento (Devin MCP)

> Send via cognition.com contact/partnerships, Devin in-app support, or @cognition on X.
> Keep it short; partnerships teams skim. Swap in your name/role and any usage numbers.

---

**Subject:** Community MCP for Devin — "memento": Devin that improves its own skills overnight

Hi Cognition team,

I built **memento**, an open-source MCP server for Devin that gives it a nightly
**self-improvement loop**: it harvests a team's Devin sessions, mines recurring
tasks, proposes bounded edits to a long-term `SKILL.md`, and only adopts a change
when it **strictly beats a held-out validation gate** (built on Microsoft's
SkillOpt). It also ships a built-in memory engine — hybrid BM25 + vector search,
memory tiers, a knowledge graph, auto-derived "lessons," and a local dashboard.

It's live and easy to try:
- Repo: https://github.com/xerxes-y/memento
- PyPI: `uvx devin-memento` — https://pypi.org/project/devin-memento/
- One-click bundle: `.mcpb` on the v0.5.0 release
- Today it installs via *Settings → Connections → MCP servers → Add a custom MCP*
  (Command `uvx`, Args `["devin-memento"]`).

I'd love to explore a **Marketplace listing** so Devin teams can find it. One
honest note: it's a **local / STDIO** server (it reads local Devin transcripts
and runs a local optimizer), which differs from the hosted/OAuth servers in the
gallery today. Happy to discuss whether that fits the curated list as-is, or to
build a hosted variant if that's the bar.

Could we find 15 minutes? I can walk through a live demo.

Thanks,
<your name> — <role / company / Devin org>
