from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from melone_service.mcp import tools


# Tool registration only — signatures and docstrings are the tool schema
# consumed by AI agents; the query logic lives in tools.py as plain functions.

mcp = FastMCP(
    "melone",
    instructions=(
        "Melone passively records the user's desktop activity — apps, window"
        " titles, browser URLs, and (when enabled) indexed on-screen text — in"
        " a local SQLite database. You have one read-only tool:"
        " search_contexts.\n\n"
        "Call search_contexts whenever the user refers to something they"
        " recently saw, worked on, or read on screen, and you need to find the"
        " page, app, URL, or indexed screen text. Queries can be exact keywords"
        " or natural-language descriptions. Do not guess URLs or filenames;"
        " search first.\n\n"
        "Example situations that should trigger a search:\n"
        "- \"that GitHub PR I was reviewing\" → query: \"pull\" or repo/PR keyword\n"
        "- \"the Notion doc about onboarding\" → query: \"onboarding\"\n"
        "- \"where did I see that stack trace?\" → query: error class or message fragment\n"
        "- \"open the API docs I had in Chrome\" → query: \"api\" or product name\n"
        "- \"what spreadsheet had the Q3 numbers?\" → query: \"Q3\" or sheet title\n"
        "- user asks you to continue a task they left open earlier → query: task keyword\n\n"
        "Responses include {\"available\": bool, ...}. When available is false,"
        " read \"reason\" — the Melone collector is probably not running."
        " Tell the user to start Melone; do not retry in a tight loop."
    ),
)


@mcp.tool()
def search_contexts(
    query: str,
    limit: int = tools.DEFAULT_SEARCH_LIMIT,
    since_minutes: int = tools.DEFAULT_SEARCH_SINCE_MINUTES,
) -> dict[str, object]:
    """Search the user's recent desktop activity for a keyword, phrase, or sentence.

    When to call:
    Use this tool whenever you need to *find* something the user already looked
    at — a web page, document, app window, or on-screen text — rather than
    inventing a link from memory. Prefer calling it early when the user's
    request depends on their recent work context.

    Good use cases (call with a short, specific query derived from the request):
    - User says "that PR about auth I was reading" → query: "auth" or PR number
    - User says "the design doc from this morning" → query: "design" (widen
      since_minutes if needed)
    - User says "where did I see Connection refused?" → query: "Connection refused"
    - User asks you to summarize "the wiki page I had open" → query: wiki title
      or topic keyword
    - User wants to resume work: "continue the melone config change" → query:
      "melone" or "config"
    - User mentions a meeting doc, Slack thread, Figma file, or dashboard they
      visited → query: distinctive word from the title or URL fragment

    Do NOT call when:
    - You already have an explicit URL or file path from the user.
    - The question is general knowledge unrelated to the user's screen history.
    - You only need the *current* frontmost window (this tool searches history).

    How matching works:
    Matching blends case-insensitive label/URL matches, indexed screen text
    BM25 matches, optional semantic screen-text matches for natural-language
    queries, and attention/PageRank scores. Semantic matching is used only when
    enabled and available; otherwise search falls back to label/URL plus
    BM25 screen text and PageRank without requiring a model download.

    Parameters:
    - query (required): non-blank keyword, short phrase, or natural-language
      sentence. Extract distinctive exact terms for code/URLs/errors, or use a
      concise sentence when the user describes screen text by meaning. A blank
      query is rejected.
    - limit (default 5): max ranked results.
    - since_minutes (default 1440 = 24 hours): lookback window. Increase for
      older recall (e.g. 4320 for ~3 days, 10080 for ~7 days).

    Response when the database is ready:
    {
      "available": true,
      "results": [
        {
          "key": string,          // internal id; for dedup, not display
          "kind": "url" | "app_window" | "app",
          "label": string,        // human-readable title for the user
          "uri": string | null,   // openable URL when kind is a web page
          "score": float,         // higher = better match + more attention
          "visits": int,
          "lastSeenAt": string,   // ISO-8601 UTC
          "matchSource": string | omitted,
            // "ocr" — on-screen text only
            // "context+ocr" — label/URL and on-screen text
            // semantic screen-text matches also report as "ocr" in v1
            // omitted — label or URL only
          "snippet": string | omitted  // OCR excerpt when applicable
        },
        ...
      ],
      "episodes": [
        {
          "startedAt": string,    // ISO-8601 UTC — when the user was on this page
          "endedAt": string | null,
          "app": string | null,
          "window": string | null,
          "url": string | null,
          "matchSource": "ocr" | omitted,
          "snippet": string | omitted
        },
        ...
      ]
    }

    results are sorted by score descending. episodes (up to 10, newest first)
    answer "when did the user see this?" — cite them when timing matters.

    After a successful search:
    - Present the top result's label (and uri if present) to the user.
    - Use uri directly when you need to open or fetch a web page.
    - If results are empty, try a shorter or alternate keyword, or widen
      since_minutes once before telling the user nothing was found.

    When the database is not ready:
    {"available": false, "reason": "...", "results": [], "episodes": []}

    Screen-text matches require Screen Text Search to be enabled by the user.
    Empty results with available=true means no match in the lookback window.
    """
    return tools.search_contexts(
        query=query,
        limit=limit,
        since_minutes=since_minutes,
    )


def main() -> None:
    # stdout is reserved for the MCP protocol — diagnostics must go to stderr.
    mcp.run(transport="stdio")
