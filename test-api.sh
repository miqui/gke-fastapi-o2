#!/usr/bin/env bash
# Smoke-tests the message REST API end to end. Exits non-zero on the first failed check.
# Usage: ./test-api.sh            (against the kind cluster's ingress, http://localhost)
#        BASE_URL=http://localhost:8080 ./test-api.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost}"

command -v jq >/dev/null 2>&1 || { echo "Error: jq is required."; exit 1; }

echo "=========================================================="
echo " Testing FastAPI + SQLAlchemy + PostgreSQL REST API"
echo " Base URL: ${BASE_URL}"
echo "=========================================================="

BODY=""
STATUS=""
FAILURES=0

# api METHOD PATH [JSON_BODY] - sets STATUS and BODY, prints the exchange.
api() {
  local method="$1" path="$2" data="${3:-}" out
  if [ -n "${data}" ]; then
    out=$(curl -s -w '\n%{http_code}' -X "${method}" "${BASE_URL}${path}" \
      -H 'Content-Type: application/json' -d "${data}")
  else
    out=$(curl -s -w '\n%{http_code}' -X "${method}" "${BASE_URL}${path}")
  fi
  STATUS=$(printf '%s' "${out}" | tail -n1)
  BODY=$(printf '%s' "${out}" | sed '$d')
  echo "${method} ${path} -> ${STATUS}"
  if [ -n "${BODY}" ]; then echo "${BODY}" | jq . 2>/dev/null || echo "${BODY}"; fi
}

# expect DESCRIPTION EXPECTED ACTUAL
expect() {
  if [ "$2" = "$3" ]; then
    echo "  ok: $1"
  else
    echo "  FAIL: $1 (expected '$2', got '$3')"
    FAILURES=$((FAILURES + 1))
  fi
}

echo -e "\n1. Liveness / readiness:"
api GET /health/liveness
expect "liveness is 200" 200 "${STATUS}"
api GET /health/readiness
expect "readiness is 200" 200 "${STATUS}"
expect "readiness reports UP" UP "$(echo "${BODY}" | jq -r .status)"

echo -e "\n2. List messages:"
api GET '/messages?limit=50&offset=0'
expect "list is 200" 200 "${STATUS}"
expect "list has totalCount" number "$(echo "${BODY}" | jq -r '.totalCount | type')"

echo -e "\n3. Create an author:"
EMAIL="kubernetes-admin-$(date +%s)@example.com"
api POST /authors "{\"name\":\"kubernetes-admin\",\"email\":\"${EMAIL}\"}"
expect "create author is 201" 201 "${STATUS}"
AUTHOR_ID=$(echo "${BODY}" | jq -r .id)

echo -e "\n4. Create a valid message:"
api POST /messages "{\"title\":\"Kubernetes Kind Deployment\",\"content\":\"FastAPI running on the kind cluster!\",\"authorId\":\"${AUTHOR_ID}\"}"
expect "create message is 201" 201 "${STATUS}"
MSG_ID=$(echo "${BODY}" | jq -r .id)
expect "new message is at version 0" 0 "$(echo "${BODY}" | jq -r .version)"

echo -e "\n5. Create an invalid message (expecting 400 BAD_USER_INPUT with invalidParams):"
api POST /messages '{"title":"","content":"","authorId":""}'
expect "invalid create is 400" 400 "${STATUS}"
expect "error code is BAD_USER_INPUT" BAD_USER_INPUT "$(echo "${BODY}" | jq -r .code)"
expect "all three fields are reported" 3 "$(echo "${BODY}" | jq '.invalidParams | length')"

echo -e "\n6. Get message by id (${MSG_ID}):"
api GET "/messages/${MSG_ID}"
expect "get is 200" 200 "${STATUS}"
expect "id matches" "${MSG_ID}" "$(echo "${BODY}" | jq -r .id)"

echo -e "\n7. Update message, version 0:"
api PATCH "/messages/${MSG_ID}" '{"title":"Updated Title","content":"Updated content for the kind cluster","version":0}'
expect "update is 200" 200 "${STATUS}"
expect "version is bumped to 1" 1 "$(echo "${BODY}" | jq -r .version)"

echo -e "\n8. Update again with a stale version (expecting 409 CONFLICT):"
api PATCH "/messages/${MSG_ID}" '{"content":"Stale write","version":0}'
expect "stale update is 409" 409 "${STATUS}"
expect "error code is CONFLICT" CONFLICT "$(echo "${BODY}" | jq -r .code)"

echo -e "\n9. Unknown id and malformed id:"
api GET /messages/00000000-0000-0000-0000-000000000000
expect "unknown id is 404" 404 "${STATUS}"
api GET /messages/not-a-uuid
expect "malformed id is 400" 400 "${STATUS}"

echo -e "\n10. Delete the author while it still has a message (expecting 409):"
api DELETE "/authors/${AUTHOR_ID}"
expect "delete author with messages is 409" 409 "${STATUS}"

echo -e "\n11. Delete message (${MSG_ID}):"
api DELETE "/messages/${MSG_ID}"
expect "delete message is 204" 204 "${STATUS}"

echo -e "\n12. Delete author (${AUTHOR_ID}), now that its message is gone:"
api DELETE "/authors/${AUTHOR_ID}"
expect "delete author is 204" 204 "${STATUS}"

echo -e "\n=========================================================="
if [ "${FAILURES}" -eq 0 ]; then
  echo " API verification passed."
  echo "=========================================================="
else
  echo " API verification FAILED: ${FAILURES} check(s)."
  echo "=========================================================="
  exit 1
fi
