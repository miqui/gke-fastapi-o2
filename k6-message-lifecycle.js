import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

export const options = {
  vus: __ENV.VUS ? parseInt(__ENV.VUS, 10) : 10,
  duration: __ENV.DURATION || '10s',
  thresholds: {
    http_req_failed: ['rate<0.01'],    // Error rate under 1%
    http_req_duration: ['p(95)<500'],  // 95% of requests below 500ms
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost';
const jsonHeaders = { 'Content-Type': 'application/json', 'Accept': 'application/json' };

export function setup() {
  const res = http.post(`${BASE_URL}/authors`, JSON.stringify({
    name: 'k6-message-lifecycle',
    email: `k6-message-lifecycle-${Date.now()}@example.com`,
  }), { headers: jsonHeaders });

  if (res.status !== 201) {
    throw new Error(`setup: failed to create author, status ${res.status}, body ${res.body}`);
  }
  return { authorId: res.json().id };
}

export default function (data) {
  // Create
  const createRes = http.post(`${BASE_URL}/messages`, JSON.stringify({
    title: `Lifecycle message ${randomIntBetween(1, 1000000)}`,
    content: 'Created by k6-message-lifecycle.js',
    authorId: data.authorId,
  }), { headers: jsonHeaders, tags: { name: 'CreateMessage' } });
  const created = check(createRes, {
    'create: status is 201': (r) => r.status === 201,
  });
  if (!created) {
    sleep(randomIntBetween(1, 3) * 0.1);
    return;
  }
  const id = createRes.json().id;

  // Read
  const getRes = http.get(`${BASE_URL}/messages/${id}`, { tags: { name: 'GetMessageById' } });
  check(getRes, {
    'read: status is 200': (r) => r.status === 200,
    'read: id matches': (r) => r.json().id === id,
  });

  // Update - version 0 matches the just-created message (see MessageUpdate.version in
  // app/schemas.py / update_message in app/services/messages.py: the server rejects a stale
  // version with 409 CONFLICT).
  const updateRes = http.patch(`${BASE_URL}/messages/${id}`, JSON.stringify({
    title: 'Updated by k6-message-lifecycle.js',
    content: 'Updated content',
    version: 0,
  }), { headers: jsonHeaders, tags: { name: 'UpdateMessage' } });
  check(updateRes, {
    'update: status is 200': (r) => r.status === 200,
    'update: version bumped to 1': (r) => r.json().version === 1,
  });

  // Delete
  const deleteRes = http.del(`${BASE_URL}/messages/${id}`, null, { tags: { name: 'DeleteMessage' } });
  check(deleteRes, {
    'delete: status is 204': (r) => r.status === 204,
  });

  // Simulate think time between 100ms and 300ms using k6-utils
  sleep(randomIntBetween(1, 3) * 0.1);
}
