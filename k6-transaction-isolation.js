import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// Proves PATCH /messages/{id}'s optimistic locking (update_message in
// app/services/messages.py) actually closes the lost-update gap for real HTTP clients: many VUs
// race to increment a counter stored in one message's `content` field, each doing its own read
// (content + version) then a PATCH that submits content+1 guarded by the version it read. The
// server answers 409 whenever the row changed since that version was read - so out of N
// attempts, some legitimately lose the race and get 409 (expected, not a bug), but every success
// must correspond to a real, distinct +1.
//
// The invariant checked in teardown: every successful PATCH bumps `version` by exactly 1 and
// sets content = (content it read) + 1. Starting from content "0" / version 0, the two therefore
// stay equal if and only if no write was applied against a stale read. Under a server-side-only
// CAS (re-reading its own "current" version right before writing instead of trusting the version
// the client read) they diverge: the DB's version outruns the counter.
export const options = {
  vus: __ENV.VUS ? parseInt(__ENV.VUS, 10) : 20,
  duration: __ENV.DURATION || '15s',
  thresholds: {
    // 409 is an expected outcome (see setResponseCallback below), so it isn't a failure.
    http_req_failed: ['rate<0.01'],
    // Teardown: final content == final version. Any lost update fails the run.
    no_lost_updates: ['rate==1'],
  },
};

// 200-299 and 409 are expected here; anything else (5xx, 404, 400) counts as failed.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 299 }, 409));

const BASE_URL = __ENV.BASE_URL || 'http://localhost';
const jsonHeaders = { 'Content-Type': 'application/json', 'Accept': 'application/json' };

const successfulIncrements = new Counter('successful_increments');
const writeConflicts = new Counter('write_conflicts');
const finalCounterValue = new Trend('final_counter_value');
const noLostUpdates = new Rate('no_lost_updates');

export function setup() {
  const authorRes = http.post(`${BASE_URL}/authors`, JSON.stringify({
    name: 'k6-isolation-test',
    email: `k6-isolation-test-${Date.now()}@example.com`,
  }), { headers: jsonHeaders });
  if (authorRes.status !== 201) {
    throw new Error(`setup: failed to create author, status ${authorRes.status}, body ${authorRes.body}`);
  }
  const authorId = authorRes.json().id;

  const createRes = http.post(`${BASE_URL}/messages`, JSON.stringify({
    title: 'k6-transaction-isolation counter',
    content: '0',
    authorId,
  }), { headers: jsonHeaders });
  if (createRes.status !== 201) {
    throw new Error(`setup: failed to create counter message, status ${createRes.status}, body ${createRes.body}`);
  }
  return { id: createRes.json().id };
}

export default function (data) {
  const getRes = http.get(`${BASE_URL}/messages/${data.id}`, { tags: { name: 'ReadCounter' } });
  const read = check(getRes, {
    'read: status is 200': (r) => r.status === 200,
  });
  if (!read) return;

  const current = parseInt(getRes.json().content, 10);
  const readVersion = getRes.json().version;

  const patchRes = http.patch(`${BASE_URL}/messages/${data.id}`, JSON.stringify({
    content: String(current + 1),
    version: readVersion,
  }), { headers: jsonHeaders, tags: { name: 'IncrementCounter' } });

  const isSuccess = patchRes.status === 200;
  const isConflict = patchRes.status === 409;

  check(patchRes, {
    'write: success or 409 CONFLICT': () => isSuccess || isConflict,
  });

  if (isSuccess) {
    successfulIncrements.add(1);
  } else if (isConflict) {
    writeConflicts.add(1);
  }
}

export function teardown(data) {
  // Reads are cache-aside (see app/cache.py). A slow reader can repopulate the cache with a
  // pre-update row right after an update's eviction; the next stale-version 409 evicts it again,
  // but no writers are left at the tail of a burst to do that. Poll until two consecutive reads
  // agree so the check runs against settled state rather than a transient stale entry.
  let content = NaN;
  let version = NaN;
  let previous = null;
  for (let attempt = 0; attempt < 20; attempt++) {
    const res = http.get(`${BASE_URL}/messages/${data.id}`);
    if (res.status === 200) {
      content = parseInt(res.json().content, 10);
      version = res.json().version;
      const snapshot = `${content}/${version}`;
      if (snapshot === previous) break;
      previous = snapshot;
    }
    sleep(0.3);
  }
  finalCounterValue.add(content);
  noLostUpdates.add(content === version);

  console.log(`\nFinal state: counter (content) = ${content}, version = ${version}.`);
  console.log('They must be equal. Compare both against "successful_increments" in the summary '
    + 'below: also exactly equal. "write_conflicts" (409s) are expected under contention and are '
    + 'not lost updates - the client is told to refetch and retry.');

  http.del(`${BASE_URL}/messages/${data.id}`);
}
