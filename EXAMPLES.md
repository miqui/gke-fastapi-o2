# API Examples

Example `curl` calls against the message-service REST API, ordered simple to complex. All of them
target the local ingress at `http://localhost` (use `http://localhost:8080` for a local
`python -m app` run). See [README.md](README.md#rest-api) for the endpoint summary and
[API-DESIGN.md](API-DESIGN.md) for the status codes, validation rules and error model. With
`API_DOCS_ENABLED=true` the same API is browsable at `http://localhost/docs`.

Replace placeholder IDs (`<AUTHOR_ID>`, `<MESSAGE_ID>`) with real ones from your own data - run
example 1 first to find some. Every example uses `jq` to pretty-print; drop `| jq` if you don't have it.

```bash
BASE=http://localhost
```

---

### 1. List authors

```bash
curl -s "$BASE/authors" | jq
```

```json
{
  "items": [
    { "id": "1030e3d2-d6df-490c-b9f6-828f9770cb0e", "name": "system",
      "email": "system@message-service.local", "createdAt": "2026-09-24T03:03:04.720108Z" }
  ],
  "totalCount": 1
}
```

Authors are paginated like messages (`?limit=50&offset=0` by default), oldest first.

### 2. Get a single author by ID

```bash
curl -s "$BASE/authors/<AUTHOR_ID>" | jq
```

### 3. Get a single message by ID

```bash
curl -s "$BASE/messages/<MESSAGE_ID>" | jq
```

The response embeds the author. The first read of a message loads it from Postgres and populates the
Hazelcast cache; later reads are served from the cache until the message is updated or deleted.

### 4. Create an author

```bash
curl -s -i -X POST "$BASE/authors" \
  -H 'Content-Type: application/json' \
  -d '{"name": "Ada Lovelace", "email": "ada@example.com"}'
```

Answers `201 Created` with a `Location: /authors/<id>` header and the created author. A second author
with the same email is a `409 CONFLICT`.

### 5. Create a message

```bash
curl -s -i -X POST "$BASE/messages" \
  -H 'Content-Type: application/json' \
  -d '{"title": "Hello", "content": "First message", "authorId": "<AUTHOR_ID>"}'
```

Answers `201 Created` with `Location: /messages/<id>`; the new message is at `"version": 0`. An unknown
`authorId` is a `404 NOT_FOUND`.

### 6. Paginate messages - first page

```bash
curl -s "$BASE/messages?limit=5&offset=0" | jq '{totalCount, ids: [.items[].id]}'
```

Newest first. `totalCount` is the total number of messages, independent of `limit`/`offset`.

### 7. Paginate messages - next page

```bash
curl -s "$BASE/messages?limit=5&offset=5" | jq '{totalCount, ids: [.items[].id]}'
```

`limit` must be `1`-`200` and `offset` must be `>= 0`; anything else is a `400` (see example 10).

### 8. Update a message with optimistic locking

Send back the `version` you read. `title` is optional; `content` and `version` are required.

```bash
curl -s -X PATCH "$BASE/messages/<MESSAGE_ID>" \
  -H 'Content-Type: application/json' \
  -d '{"title": "Hello (edited)", "content": "Edited content", "version": 0}' | jq '{version, title}'
```

```json
{ "version": 1, "title": "Hello (edited)" }
```

Repeat the same request: the row is now at version 1, so version 0 is stale and the update is refused
without touching the row.

```bash
curl -s -i -X PATCH "$BASE/messages/<MESSAGE_ID>" \
  -H 'Content-Type: application/json' \
  -d '{"content": "Stale write", "version": 0}'
```

```
HTTP/1.1 409 Conflict
content-type: application/problem+json

{"type":"/problems/conflict","title":"Conflict","status":409,
 "detail":"Message with ID '...' has changed since version 0 was read; refetch and retry.",
 "instance":"/messages/<ID>","code":"CONFLICT"}
```

### 9. An author with their messages

```bash
curl -s "$BASE/authors/<AUTHOR_ID>?include=messages" | jq '{name, messages: [.messages[].title]}'
```

Without `?include=messages` the `messages` key is omitted. The embedded messages don't repeat the
author, so there is no author -> messages -> author nesting.

### 10. Error responses

Every error is `application/problem+json` with a stable `code`. Validation failures list every bad
field at once:

```bash
curl -s -X POST "$BASE/messages" -H 'Content-Type: application/json' \
  -d '{"title": "", "content": "", "authorId": "not-a-uuid"}' | jq
```

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
    { "name": "content", "reason": "content is required and cannot be blank" },
    { "name": "authorId", "reason": "authorId must be a valid UUID" }
  ]
}
```

Other cases:

```bash
# 404 NOT_FOUND - a well-formed id that doesn't exist
curl -s -i "$BASE/messages/00000000-0000-0000-0000-000000000000"

# 400 BAD_USER_INPUT - a malformed id (invalidParams names "id")
curl -s -i "$BASE/messages/not-a-uuid"

# 400 BAD_USER_INPUT - out-of-range pagination (never silently clamped)
curl -s -i "$BASE/messages?limit=500&offset=-1"

# 400 BAD_USER_INPUT - malformed JSON
curl -s -i -X POST "$BASE/messages" -H 'Content-Type: application/json' -d '{"title": '

# 409 CONFLICT - deleting an author that still has messages
curl -s -i -X DELETE "$BASE/authors/<AUTHOR_ID>"
```

### 11. Full lifecycle in one script

```bash
#!/usr/bin/env bash
set -euo pipefail
BASE=http://localhost

# Create an author and a message.
AUTHOR_ID=$(curl -s -X POST "$BASE/authors" -H 'Content-Type: application/json' \
  -d "{\"name\": \"Demo\", \"email\": \"demo-$(date +%s)@example.com\"}" | jq -r .id)
MSG_ID=$(curl -s -X POST "$BASE/messages" -H 'Content-Type: application/json' \
  -d "{\"title\": \"Demo\", \"content\": \"Hello\", \"authorId\": \"$AUTHOR_ID\"}" | jq -r .id)

# Read it, update it (version 0 -> 1), read it again.
curl -s "$BASE/messages/$MSG_ID" | jq '{title, version}'
curl -s -X PATCH "$BASE/messages/$MSG_ID" -H 'Content-Type: application/json' \
  -d '{"content": "Hello again", "version": 0}' | jq '{content, version}'
curl -s "$BASE/messages/$MSG_ID" | jq '{content, version}'

# Walk every page of messages.
OFFSET=0; LIMIT=50
while :; do
  PAGE=$(curl -s "$BASE/messages?limit=$LIMIT&offset=$OFFSET")
  echo "$PAGE" | jq -r '.items[].title'
  TOTAL=$(echo "$PAGE" | jq .totalCount)
  OFFSET=$((OFFSET + LIMIT))
  [ "$OFFSET" -ge "$TOTAL" ] && break
done

# Clean up: the message first, then its author (409 while any message remains).
curl -s -o /dev/null -w 'delete message: %{http_code}\n' -X DELETE "$BASE/messages/$MSG_ID"
curl -s -o /dev/null -w 'delete author:  %{http_code}\n' -X DELETE "$BASE/authors/$AUTHOR_ID"
```
