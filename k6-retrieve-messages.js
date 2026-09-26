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

export default function () {
  const res = http.get(`${BASE_URL}/messages?limit=50`, { tags: { name: 'GetAllMessages' } });

  check(res, {
    'status is 200': (r) => r.status === 200,
    'has at least one item': (r) => {
      try {
        return r.json().items.length > 0;
      } catch (e) {
        return false;
      }
    },
    'has totalCount': (r) => {
      try {
        return typeof r.json().totalCount === 'number';
      } catch (e) {
        return false;
      }
    },
  });

  // Paginate with limit/offset (see GET /messages in app/routers/messages.py): a small page
  // should come back at most that size (limit is bounded to 1-200 server-side).
  const pageRes = http.get(`${BASE_URL}/messages?limit=5`, { tags: { name: 'GetMessagesPage' } });
  check(pageRes, {
    'page: status is 200': (r) => r.status === 200,
    'page: at most 5 items': (r) => {
      try {
        return r.json().items.length <= 5;
      } catch (e) {
        return false;
      }
    },
  });

  // Simulate think time between 100ms and 300ms using k6-utils
  sleep(randomIntBetween(1, 3) * 0.1);
}
