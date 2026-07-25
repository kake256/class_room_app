# Course overview bulk contract

`grader.course_overview.compose_course_overview` combines an already-authorized course's normalized Classroom coursework list with local settings and readiness in one response-shaped mapping. It exists to replace the UI's per-coursework settings/readiness HTTP calls without coupling the domain logic to FastAPI or the browser.

The integration is exposed as `GET /api/v1/courses/{course_id}/overview`. It authenticates the session, validates teacher membership, fetches Classroom courseworks once, normalizes them to `id`, `title`, `due_date`, `max_points`, and `assignment_key`, then calls:

```python
overview = compose_course_overview(_cfg, selected_course_id, normalized_courseworks)
```

The response contract is `{"course_id": ..., "courseworks": [...]}`. Every coursework contains its normalized metadata plus `settings`, counts-only `readiness`, `configured`, and `overview_errors`. Input order is retained. `configured` intentionally matches the current UI: when a settings file exists its `confirmed` value wins; the legacy assignment mapping is considered only when settings are absent.

Each settings/readiness dependency is called at most once for each unique coursework during the bulk operation. Missing settings remain `None`; existing readiness semantics define zero counts and stale metadata. A corrupt or unreadable coursework does not fail its siblings and returns only `settings_unavailable` or `readiness_unavailable`; exception text, paths, student rows, names, IDs, and submissions are never included.

The endpoint must perform course authorization before this call. The module does not cache, so token changes, course changes, settings revisions, metadata, result, and report updates are visible on the next request without an invalidation protocol. If caching is added later, all of those values must form the cache boundary or invalidate it.

The browser uses this one endpoint per selected course and does not issue per-coursework settings/readiness requests. The legacy individual endpoints remain available for settings editing and compatibility. MCP `list_courseworks` uses the same overview response, whose readiness contains counts/status only and no row-level student information.

On 2026-07-20, the 251 Docker test image composed 100 mock courseworks 1,000 times in 0.165 seconds total (about 0.165 ms per overview). This measures only the pure composition layer; real latency is dominated by the single Classroom list request and local settings/readiness reads.
