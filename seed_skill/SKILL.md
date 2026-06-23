---
name: skillopt-sleep-learned
description: Auto-evolved skill — optimised nightly by SkillOpt-Sleep from past Devin sessions. Covers recurring patterns in Java/Spring Boot development, team workflow, Robot Framework tests, and database analysis.
---

# SkillOpt-Sleep Learned Skill

This skill is automatically updated by SkillOpt-Sleep. The content below is the
initial seed and will be replaced with validation-gated improvements after the
first sleep cycle runs.

## Current Patterns

### Java / Spring Boot
- Always check for `@Transactional` propagation when persistence methods are called across service boundaries.
- Prefer `Optional.orElseThrow(NotFoundException::new)` over `.get()` on repository results.
- Use `@Slf4j` (Lombok) for logging; never use `System.out.println` in production code.
- Repository query methods returning lists should use `List<T>`, never `Iterable<T>`, for downstream compatibility.

### Team Workflow
- Before implementing a Jira story, read the ticket description AND linked acceptance criteria in full.
- Robot Framework keywords go in `api_keywords.robot`; helper setup/teardown logic stays in `resource_keywords.robot`.
- Always run `rtk mvn test` locally before pushing; failing tests block the pipeline.
- MR descriptions must include: what changed, why, how to test, and any migration notes.

### Database Analysis (MariaDB)
- Use `SELECT ... LIMIT 20` for first-pass exploration; never `SELECT *` on large tables.
- When joining across schemas, verify foreign-key alignment on the shared key before querying.
- The `agentmemory` MCP tool stores findings from past queries — check it before re-running expensive queries.

### Code Review
- Check exception handling first: bare `catch (Exception e)` without re-throw or logging is always a defect.
- N+1 query risk: any `@OneToMany` without `fetch = FetchType.LAZY` + explicit join fetch is suspect.
- Security: never log full request payloads that may contain PII or credentials.

## Evolution Notes

SkillOpt-Sleep will propose bounded edits (add / delete / replace) to this file based on:
- Recurring tasks mined from past Devin sessions
- Validation-gated replay: only accepted if held-out score improves
- Review before adoption: run `sleep_adopt` after inspecting the staged proposal
