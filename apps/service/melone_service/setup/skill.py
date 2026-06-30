"""Installs the bundled `melone` agent skill alongside the MCP server.

When the user enables the Melone MCP for an agent, we also drop the `/melone`
skill into that agent's skills dir (Claude Code: ~/.claude/skills/melone,
Codex: ~/.codex/skills/melone) so the agent has the full workflow for grounding
answers in Melone context. Unlike editing CLAUDE.md/AGENTS.md, this writes a
self-contained file we own — closer to installing a plugin than touching the
user's content. The skill text is embedded (not a data file) so it ships inside
the frozen daemon. Reuses common.create_backup / atomic_write_text for the same
crash-safety guarantees as the config editors.
"""

from contextlib import suppress
from pathlib import Path

from .common import SetupResult, atomic_write_text, create_backup

# The bundled skill. Kept in sync with docs/the canonical SKILL.md; embedded as a
# string so a packaged build needs no extra data files.
SKILL_CONTENT = """\
---
name: melone
description: Search the user's recent desktop context with the Melone MCP, answer based on what the user has been doing, and cite any Melone-derived context in a References section. Use when the user invokes /melone or $melone, asks Codex to answer from current or recent Mac activity, wants context from recently viewed apps/windows/browser pages/OCR text, or asks what they were working on.
---

# Melone

## Overview

Use Melone's read-only desktop activity tools to ground answers in the user's current and recent Mac context. Prefer concise, evidence-based answers over raw activity dumps.

## Workflow

1. Identify the user's actual question.
   - Treat text after `/melone` or `$melone` as the question to answer.
   - If the request is broad, answer from recent context and state the time window used.
   - If the request needs a specific keyword, project, person, app, URL, or topic, extract those search terms before calling tools.

2. Load Melone tools if needed.
   - If Melone tools are not already available, use tool discovery for `melone`.
   - Use only read-only Melone tools. Do not modify files, messages, apps, or browser state just to answer a Melone question.

3. Gather the smallest useful context set.
   - Call `mcp__melone.get_current_context` first for the active app, window, URL, and activity state.
   - Call `mcp__melone.rank_contexts` when the user asks what they have been focused on, what matters recently, or gives a broad question. Start with `since_minutes: 240` and `limit: 10`; widen to 1440 minutes only if needed.
   - Call `mcp__melone.search_contexts` for concrete terms from the prompt. Use multiple focused searches only when one query would mix unrelated concepts. Start with `since_minutes: 1440` and `limit: 10` unless the user gives a different time range.
   - Call `mcp__melone.get_timeline` when chronology matters, such as "what happened before this?", "what was I doing earlier?", handoff summaries, or reconstructing a work session. Use a concise window, usually 60 to 240 minutes.

4. Interpret evidence carefully.
   - Treat app/window titles, URLs, labels, visits, `lastSeenAt`, OCR snippets, and timeline events as clues, not complete truth.
   - Prefer repeated or recent signals over one-off events.
   - Mention uncertainty when context is thin, conflicting, stale, or unavailable.
   - Do not infer private intent, emotions, or sensitive facts beyond what the recorded context supports.
   - Do not expose irrelevant private context. Include only the details needed to answer the question.

5. Answer from the evidence.
   - Write in the user's language unless they ask otherwise.
   - Give the direct answer first.
   - Cite every claim that is added, corrected, or materially strengthened by Melone context with an inline reference marker such as `[M1]`, `[M2]`, or `[M3]`.
   - Do not cite ordinary reasoning, user-provided facts, or conversation-only context with Melone markers.
   - Include compact supporting evidence when helpful: app/window names, page titles, URLs, timestamps, or snippets.
   - Link returned `uri` values when they are directly relevant.
   - Add a `References` section at the end whenever Melone context was used in the answer. Omit `References` only when Melone was unavailable or no Melone result informed the answer.
   - If Melone is unavailable, say that the activity database is unavailable and answer only from normal conversation context.
   - If Melone finds no relevant context, say so plainly and ask for a keyword, time range, or project name only if needed.

## Citations and References

- Use stable inline markers in first-use order: `[M1]`, `[M2]`, `[M3]`.
- Reuse the same marker for repeated use of the same Melone result, episode, current context, or timeline cluster.
- Keep citations close to the supported claim, usually at the end of the sentence or bullet.
- Do not attach a citation to an entire paragraph if only one sentence depends on Melone.
- When Melone changes or supplements an answer that could otherwise be answered from conversation alone, cite the supplemented portion.
- End with a `References` section that maps each marker to a concise evidence item.
- Each reference should include the evidence type and enough detail to audit it: app/window title, page label, URL or returned `uri`, timestamp or time range, and OCR snippet when relevant.
- Do not include sensitive or irrelevant context in references. Summarize private snippets instead of copying them when the exact text is not necessary.

Reference format:

```text
References

[M1] Current context, {app}: "{window}", {url if available}, activity: {state}.
[M2] Melone search result, "{label}", {uri if available}, last seen {timestamp}; snippet: "{short relevant snippet}".
[M3] Melone timeline, {time range}: {short sequence of relevant apps/windows/URLs}.
```

## Common Patterns

### Current Context

For "what am I looking at?", "summarize this", or "answer based on the current screen":

1. Call `get_current_context`.
2. If the current window title or URL is not enough, search for terms visible in the user's prompt with `search_contexts`.
3. Answer with a short statement of what context was visible.

### Recent Work Summary

For "what was I working on?", "catch me up", or "summarize my recent context":

1. Call `rank_contexts` for the last 240 minutes.
2. Call `get_timeline` for the last 120 to 240 minutes if sequence matters.
3. Group the answer by project, app, or task. Avoid listing every event.

### Topic Lookup

For "what did I see about X?", "find the tab/doc/thread I was using", or "answer using recent context about X":

1. Call `search_contexts` with the strongest keywords.
2. Use results and episodes to identify the most likely pages, apps, or documents.
3. Answer with the relevant finding and include direct `uri` links when available.

## Output Style

- Be concise and useful; Melone context is supporting evidence, not the main product.
- Use bullets for summaries or multiple candidates.
- Use exact dates/times when the user asks about chronology.
- Add inline `[M#]` citations and a final `References` section for all Melone-supported additions or corrections.
- Do not include raw JSON or exhaustive event logs unless the user explicitly requests them.
- Do not claim to have read page contents beyond labels, URLs, timeline metadata, and OCR snippets returned by Melone.
"""

_SKILL_PATHS = {
    "claude-code": lambda: Path.home() / ".claude" / "skills" / "melone" / "SKILL.md",
    "codex": lambda: Path.home() / ".codex" / "skills" / "melone" / "SKILL.md",
}


def default_skill_path(target: str) -> Path:
    """Map an mcp target ("claude-code" | "codex") to its skill file path."""
    factory = _SKILL_PATHS.get(target)
    if factory is None:
        raise KeyError(target)
    return factory()


def is_skill_installed(path: Path) -> bool:
    return path.is_file()


def install_skill(path: Path) -> SetupResult:
    """Write the bundled skill. Idempotent; refreshes when the text differs."""
    if path.is_file() and path.read_text(encoding="utf-8") == SKILL_CONTENT:
        return SetupResult(changed=False, enabled=True, config_path=path)

    backup_path = create_backup(path)  # None when the file does not exist yet
    atomic_write_text(path, SKILL_CONTENT)
    return SetupResult(changed=True, enabled=True, config_path=path, backup_path=backup_path)


def uninstall_skill(path: Path) -> SetupResult:
    """Remove the skill file (and its now-empty melone/ dir). Idempotent.

    No backup: the skill is a file we own, so removal is just an uninstall —
    leaving backups would also block cleaning up the melone/ dir we created.
    """
    if not path.is_file():
        return SetupResult(changed=False, enabled=False, config_path=path)

    path.unlink()
    # Clean up the melone/ dir we created; never touch the shared skills/ parent.
    with suppress(OSError):
        path.parent.rmdir()
    return SetupResult(changed=True, enabled=False, config_path=path)
