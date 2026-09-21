import http from 'k6/http';
import crypto from 'k6/crypto';
import { check, fail } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const payload = open('./fixtures/upload.jpg', 'b');
const digest = crypto.sha256(payload, 'hex');
const endpoint = __ENV.API_URL || 'http://localhost:8081';
const vus = Number(__ENV.VUS || 20);
const uploadTime = new Trend('direct_upload_seconds');
const completed = new Rate('upload_session_completed');
export const options = {
  scenarios: { burst: { executor: 'per-vu-iterations', vus, iterations: 1, maxDuration: '2m' } },
  thresholds: { http_req_failed: ['rate<0.01'], upload_session_completed: ['rate==1'] },
};
function uuid() {
  const hex = Array.from(new Uint8Array(crypto.randomBytes(16))).map(n => n.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-4${hex.slice(13,16)}-a${hex.slice(17,20)}-${hex.slice(20)}`;
}
export function setup() {
  if (__ENV.ALLOW_BENCHMARK_WRITES !== '1') fail('Set ALLOW_BENCHMARK_WRITES=1 for a disposable stack');
  const response = http.post(`${endpoint}/api/auth/register`, JSON.stringify({name:'Upload benchmark',email:`load-${uuid()}@test.example`,password:uuid()}), {headers:{'Content-Type':'application/json'}});
  if (response.status !== 200) fail(`Registration failed: ${response.status}`);
  return {token:response.json().token};
}
export default function(data) {
  const start=Date.now();
  const headers={'Content-Type':'application/json',Authorization:`Bearer ${data.token}`,'Idempotency-Key':uuid()};
  const created=http.post(`${endpoint}/api/uploads`, JSON.stringify({filename:'upload.jpg',contentType:'image/jpeg',size:payload.byteLength,sha256:digest,title:`Concurrent photo ${__VU}`,description:'Load fixture',category:'Test',tags:'load',visibility:'PRIVATE'}),{headers,tags:{operation:'create'}});
  if (!check(created,{'created':r=>r.status===200})) { completed.add(false); return; }
  const session=created.json();
  const stored=http.put(session.uploadUrl,payload,{headers:session.headers,tags:{operation:'storage_put'},responseType:'none'});
  if (!check(stored,{'stored':r=>r.status===200})) { completed.add(false); return; }
  const response=http.post(`${endpoint}/api/uploads/${session.uploadId}/complete`,null,{headers:{Authorization:headers.Authorization},tags:{operation:'complete'}});
  completed.add(check(response,{'completed':r=>r.status===200}));
  uploadTime.add((Date.now()-start)/1000);
}
