# REST API Design

The design of the `message-service` REST API: the resources, the conventions every endpoint follows,
and the reasoning behind the parts that aren't obvious (optimistic locking, the error model, the
cache, and how the cost of a single request is bounded). The code is the source of truth for exact
shapes - with `API_DOCS_ENABLED=true` FastAPI serves the generated OpenAPI 3.1 document at
`/openapi.json` (UI at `/docs`) - and the request/response examples live in
[EXAMPLES.md](EXAMPLES.md).

## Resources

Two resources, both addressed by UUID: `Author` and `Message` (a message belongs to exactly one
author). The tables are in `app/models.py`; the DB stays `snake_case`, JSON bodies are `camelCase`.

| Method and path | Purpose | Success | Errors |
| --- | --- | --- | --- |
| `GET /messages?limit&offset` | Page of messages, newest first | 200 `{items, totalCount}` | 400 |
| `GET /messages/{id}` | One message, author embedded (cache-aside) | 200 | 400, 404 |
| `POST /messages` | Create `{title, content, authorId}` | 201 + `Location` | 400, 404 unknown author |
| `PATCH /messages/{id}` | Update `{title?, content, version}` | 200 (`version` + 1) | 400, 404, **409** stale version |
| `DELETE /messages/{id}` | Delete | 204 | 400, 404 |
| `GET /authors?limit&offset` | Page of authors, oldest first | 200 `{items, totalCount}` | 400 |
| `GET /authors/{id}[?include=messages]` | One author, optionally with their messages | 200 | 400, 404 |
| `POST /authors` | Create `{name, email}` | 201 + `Location` | 400, **409** duplicate email |
| `PATCH /authors/{id}` | Update `{name?, email?}` | 200 | 400, 404, 409 |
| `DELETE /authors/{id}` | Delete | 204 | 400, 404, **409** still has messages |
| `GET /health/liveness`, `/health/readiness` | Kubelet probes (not in the OpenAPI document) | 200 `{"status":"UP"}` | readiness: 503 `{"status":"DOWN"}` |

`PATCH` is a partial update: an absent or `null` optional field means "unchanged". A message's
`content` and `version` are always required, because the version guard needs the caller to say which
state they read.

## Conventions

- **JSON**: `camelCase` keys (`authorId`, `createdAt`, `totalCount`), ISO-8601 UTC timestamps,
  UUID strings. Request models accept the snake_case name too (`populate_by_name`), responses always
  use the alias.
- **Status codes are real.** Unlike a single-endpoint RPC style, each outcome has its own HTTP
  status, so `curl -f`, load balancers, and the `http_requests_total{status_code}` metric all see
  failures without parsing a body.
- **Bad input is 400, not 422.** FastAPI defaults to 422 for validation errors; this API overrides it,
  so "the client sent something wrong" is one status across validation, malformed JSON, a malformed
  UUID in the path, and an out-of-range pagination bound.
- **Create returns the created representation** with `201` and a `Location` header.
- **`include=messages`** on `GET /authors/{id}` embeds the author's messages *without* their author,
  so the response has no author -> messages -> author cycle to bound.

## Validation

Implemented as Pydantic v2 models in `app/schemas.py`. Every failing field is reported at once, not
just the first.

| Field | Rule |
| --- | --- |
| `title` | required, trimmed, non-blank, at most 100 characters |
| `content` | required, trimmed, non-blank, at most 1000 characters |
| `name` | required, trimmed, non-blank, at most 50 characters |
| `email` | required, trimmed, at most 100 characters, matches `^[^\s@]+@[^\s@]+\.[^\s@]+$` |
| `authorId`, path `{id}` | must be a UUID |
| `version` | integer, `0` to 2,147,483,647 |
| `limit` | integer, `1` to `200` (default `50`) |
| `offset` | integer, `0` to 2,147,483,647 (default `0`) |

On `PATCH`, an optional field that is *present* (not `null`) must pass the same check as on create - a
blank `title` is a `400`, not "leave it alone". Out-of-range pagination values are rejected, never
silently clamped: a client asking for `limit=500` should learn that its assumption is wrong.

## Error model

Every error is [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) `application/problem+json` with a
stable machine-readable `code` that clients should switch on (the human `detail` may change):

```json
{
  "type": "/problems/bad-user-input",
  "title": "Bad Request",
  "status": 400,
  "detail": "The request content was invalid or failed validation constraints.",
  "instance": "/messages",
  "code": "BAD_USER_INPUT",
  "invalidParams": [
    { "name": "title", "reason": "title is required and cannot be blank" },
    { "name": "authorId", "reason": "authorId must be a valid UUID" }
  ]
}
```

| `code` | Status | When |
| --- | --- | --- |
| `BAD_USER_INPUT` | 400 (413 for an oversized body) | Validation failure, malformed JSON, bad UUID, out-of-range pagination |
| `NOT_FOUND` | 404 | The resource (or an unknown route) doesn't exist |
| `CONFLICT` | 409 | Stale `version`, duplicate author email, deleting an author who still has messages |
| `INTERNAL_SERVER_ERROR` | 500 | Anything unhandled. Logged with the trace id; the response never contains a stack trace or exception text |

Other framework-raised statuses (for example `405`) come back in the same shape with a
`HTTP_<status>` code. Every error increments `http_errors_total{error_code=...}`.

The RFC members:

- **`type`** names the problem class as a relative URI, and each one resolves: `GET /problems`
  lists them and `GET /problems/{slug}` describes one (`bad-user-input`, `not-found`, `conflict`,
  `payload-too-large`, `internal-server-error`). An oversized body has its own `payload-too-large`
  type, although it shares the `BAD_USER_INPUT` code with the 400 case. A status with no meaning
  beyond itself (`405`) uses `about:blank`, which is what the RFC prescribes. `code` stays the
  thing to switch on; `type` is the standards-shaped equivalent.
- **`title`** is the HTTP status phrase, the same for every occurrence of a `type`.
- **`instance`** is the request path (percent-encoded). It identifies the resource that failed, not
  the individual occurrence; to correlate with logs, use the trace id in the server log line.

The OpenAPI document (when `API_DOCS_ENABLED`) lists each route's error statuses as
`application/problem+json` with a shared `Problem` schema. FastAPI's default `422
HTTPValidationError` entries are removed, since validation answers `400`.

## Optimistic locking

`Message.version` starts at `0` and every successful `PATCH` adds one. The update is one statement:

```sql
UPDATE messages SET content = :content, version = version + 1
WHERE id = :id AND version = :version RETURNING id
```

It uses the version **the client sent**, not one the server re-reads: guarding against the server's
own just-read value only protects the milliseconds between its read and its write, and can't tell
that the client acted on stale data. When no row matches, the service checks whether the id exists:
`404` if not, `409` if it does (the row moved on).

`k6-transaction-isolation.js` is the regression test: many clients increment a counter stored in one
message, and it asserts that the final `content` equals the final `version` - which holds only if no
write was applied against a stale read. Both the unit tests and the k6 script were verified to fail
when the `version` predicate is removed.

## Caching

`GET /messages/{id}` is cache-aside against a Hazelcast map (`app/cache.py`): read the cache, on a
miss load from Postgres and populate it. `PATCH` and `DELETE` evict the entry after their
transaction commits. Lists and author endpoints don't use the cache. There is no TTL and no near
cache (a near cache went stale across pods).

The one subtle case: a slow reader can load the row *before* an update and write it into the cache
*after* that update's eviction. With no TTL nothing would ever remove that entry, and clients would
keep reading the stale `version` and get `409` on every write. So a `409` also evicts the entry -
the caller's version was stale, so the cached copy may be too - and the next read reloads from the
database. The cache is **mandatory**: an unreachable Hazelcast member fails startup rather than
running without it.

## Bounding the cost of a request

There's no query language to constrain, so the cost of one request is bounded by ordinary limits:

- **Pagination** - `limit` is capped at 200, so no list response is unbounded.
- **Field lengths** - `max_length` per field (above), so a single message is at most a few KB.
- **Body size** - bodies over 16 KiB are rejected with `413` (`app/middleware.py`), by declared
  `Content-Length` and, for chunked bodies, while streaming.
- **Connection pool** - each pod has at most 10 database connections (`pool_size` 5 + `max_overflow`
  5), so 6 pods stay well under Postgres' `max_connections = 100`.
- **Relationships** - `include=messages` returns messages without their author, so there's no
  cycle for a client to expand.

## Exposure

- **API docs**: `/docs`, `/redoc` and `/openapi.json` exist only when `API_DOCS_ENABLED` is exactly
  `"true"`. Any other value except `"false"` fails startup, so a typo can't silently expose (or hide)
  them. Off by default; the disposable dev cluster turns it on.
- **CORS**: no CORS headers unless `CORS_ALLOWED_ORIGINS` lists exact origins. curl, k6 and other
  non-browser clients are unaffected.
- **Authentication**: none. This is a local dev-cluster service; a real deployment would put
  authn/authz in front of it (and rate limiting, which is out of scope here).

## Evolving the API

- Adding an optional response field or an optional request field is backwards compatible.
- `code` values are part of the contract: add new ones, don't repurpose existing ones.
- Breaking changes (a renamed field, a changed status) would go under a new path prefix (`/v2/...`);
  if the service is ever mounted behind a prefix, set FastAPI's `root_path` so the generated links
  and OpenAPI document are right.
- A new table or column is an Alembic migration (`migrations/versions/`). Migrations run at every
  container start under a Postgres advisory lock, so they must be idempotent and safe to run while
  the previous version's pods are still serving - add columns as nullable or with a default before the
  code that requires them ships.
