import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomIntBetween, uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

export const options = {
  vus: __ENV.VUS ? parseInt(__ENV.VUS, 10) : 10,
  duration: __ENV.DURATION || '10s',
  thresholds: {
    // Every request here intentionally triggers a 4xx, so http_req_failed isn't a useful signal
    // for this scenario. `checks` passing is: did the API return the *correct* status and
    // problem+json shape (code / invalidParams) every time.
    checks: ['rate>0.99'],
    http_req_duration: ['p(95)<500'],  // 95% of requests below 500ms
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost';
const jsonHeaders = { 'Content-Type': 'application/json', 'Accept': 'application/json' };

const bodyOf = (r) => {
  try {
    return r.json();
  } catch (e) {
    return {};
  }
};

export default function () {
  // Invalid create: blank title/content and a blank authorId should return 400 BAD_USER_INPUT
  // with per-field invalidParams (see app/schemas.py and app/errors.py).
  const invalidRes = http.post(`${BASE_URL}/messages`, JSON.stringify({
    title: '', content: ' ', authorId: '',
  }), {
    headers: jsonHeaders,
    tags: { name: 'CreateInvalidMessage' },
    responseCallback: http.expectedStatuses(400),
  });
  check(invalidRes, {
    'invalid create: status is 400': (r) => r.status === 400,
    'invalid create: BAD_USER_INPUT': (r) => bodyOf(r).code === 'BAD_USER_INPUT',
    'invalid create: has invalidParams': (r) => {
      const params = bodyOf(r).invalidParams;
      return Array.isArray(params) && params.length > 0;
    },
  });

  // Well-formed but non-existent id: should return 404 NOT_FOUND
  const notFoundRes = http.get(`${BASE_URL}/messages/${uuidv4()}`, {
    tags: { name: 'GetMissingMessage' },
    responseCallback: http.expectedStatuses(404),
  });
  check(notFoundRes, {
    'not found: status is 404': (r) => r.status === 404,
    'not found: NOT_FOUND': (r) => bodyOf(r).code === 'NOT_FOUND',
  });

  // Malformed id: not a UUID, so path validation fails before any lookup - 400, not 404.
  const badIdRes = http.get(`${BASE_URL}/messages/non-existent-${randomIntBetween(1, 1000000)}`, {
    tags: { name: 'GetMessageBadId' },
    responseCallback: http.expectedStatuses(400),
  });
  check(badIdRes, {
    'bad id: status is 400': (r) => r.status === 400,
    'bad id: names the id param': (r) => {
      const params = bodyOf(r).invalidParams;
      return Array.isArray(params) && params.some((p) => p.name === 'id');
    },
  });

  // Malformed JSON body: 400, not a 500.
  const badJsonRes = http.post(`${BASE_URL}/messages`, '{"title": ', {
    headers: jsonHeaders,
    tags: { name: 'CreateMalformedJson' },
    responseCallback: http.expectedStatuses(400),
  });
  check(badJsonRes, {
    'bad json: status is 400': (r) => r.status === 400,
    'bad json: BAD_USER_INPUT': (r) => bodyOf(r).code === 'BAD_USER_INPUT',
  });

  // Simulate think time between 100ms and 300ms using k6-utils
  sleep(randomIntBetween(1, 3) * 0.1);
}
