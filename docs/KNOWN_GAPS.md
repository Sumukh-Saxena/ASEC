# Known gaps

Accepted for the MVP; each is a deliberate limit, not a hidden defect.

- Reactions are deferred (FR-ING-07). REST polling cannot see reaction removals.
- EMPTY_CONTENT is counted and alerted, but it is not flagged in a column.
- Time-zone handling has a fixed-offset fallback; it does not use a full tz database.
- Relevance and proposal artifacts hash different text (see the W8 A5 report).
- W8 A5 hash finding: the proposal request hash covers the text without `mention_handle_map` while the stored artifact includes it; the response hash covers the full text but `raw_response` may be truncated at 64 KB, so neither hash can be recomputed from storage alone.
- There is no live loop until W8.
- There is no logging setup until W9.
