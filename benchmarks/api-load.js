import http from 'k6/http';
import { check, sleep } from 'k6';

const endpoint = __ENV.API_URL || 'http://localhost:8081';
const concurrency = Number(__ENV.VUS || 20);
export const options = {
  vus: concurrency,
  duration: __ENV.DURATION || '30s',
  thresholds: { http_req_failed: ['rate<0.01'] },
};

export default function () {
  const headers = __ENV.TOKEN ? { Authorization: `Bearer ${__ENV.TOKEN}` } : {};
  const mode = __ENV.SCENARIO || 'gallery';
  let response;
  if (mode === 'team') {
    if (!__ENV.TEAM_ID || !__ENV.TOKEN) throw new Error('Team load requires TEAM_ID and TOKEN');
    response = http.get(`${endpoint}/api/teams/${__ENV.TEAM_ID}/images`, { headers });
  } else if (mode === 'semantic') {
    response = http.get(`${endpoint}/api/search?q=orange%20city&mode=semantic&scope=public`, { headers });
  } else {
    response = http.get(`${endpoint}/api/public/images?page=0&size=24`, { headers });
  }
  check(response, { 'request succeeds': r => r.status === 200 });
  sleep(0.05);
}
