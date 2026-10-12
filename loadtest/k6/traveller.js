// One traveller's visit, as the site makes its requests (Phase 30).
//
//     k6-sse run -e RATE=0.5 -e HOLD=150 loadtest/k6/traveller.js        (python scripts/loadtest.py run does this)
//
// A visit is what src/frontend does for someone who comes to plan a trip:
//
//     sign in                      POST /auth/login          half the visits: a login lasts a day
//     the trips list               GET  /trips
//     create a trip                POST /trips               the server starts warming its searches
//     the trip's page loads        GET  /trips/{id}, GET /trips/{id}/status
//     …and opens its event stream  GET  /trips/{id}/stream   ?token=, as a browser's EventSource must
//     the traveller writes         a few seconds
//     ask for the plan             POST /trips/{id}/plan     answered 202 at once
//     the plan is made             events on the stream; GET /trips/{id}/status every 3 s meanwhile
//     planning_complete            GET  /trips/{id}/itinerary and /trips/{id} together, then /status, /similar
//     the traveller reads          the stream stays open, as it does for the life of the page
//
// Visits START at a fixed rate, whatever the server does (an open model): when it slows down, the
// visits pile up — as people do — instead of politely waiting their turn.
//
// How long a plan takes is `plan_duration`: from POST /plan to `planning_complete` on the stream.
// The request that asks for a plan is answered in milliseconds and says nothing about it.
//
// Three things about k6 that shaped this file, each found by trying (docs/phase30_build_log.md):
//
// - k6 has no event streams. `http.get` on one waits for a response that never ends, and reports a
//   timeout. The stream is read by the xk6-sse extension (`k6/x/sse`), which needs a k6 built with it.
// - While a stream is open nothing else in the script runs but its event handlers — no timers. So
//   the page's "ask /status every 3 s" cannot be asked on the clock: when an event arrives, /status
//   is asked as many times as the page would have asked by then. As many requests, in bursts.
// - The extension reports a stream's whole life as the duration of an HTTP request. Streams are
//   tagged `kind:stream` and every other request `kind:api`: the latency figures are of `kind:api`.

import http from 'k6/http';
import sse from 'k6/x/sse';
import exec from 'k6/execution';
import { sleep } from 'k6';
import { SharedArray } from 'k6/data';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE = (__ENV.BASE_URL || 'http://localhost:8200').replace(/\/+$/, '');
const RATE = parseFloat(__ENV.RATE || '0.25'); // visits started a second
const HOLD = parseInt(__ENV.HOLD || '120', 10); // for this many seconds
const SIGN_IN_SHARE = parseFloat(__ENV.SIGN_IN_SHARE || '0.5');
const GIVE_UP_AFTER = parseInt(__ENV.GIVE_UP_AFTER || '180', 10); // a traveller waits this long for a plan, in seconds
const SMOKE = __ENV.SMOKE === '1'; // one traveller, three visits: does the script run cleanly

// The accounts, made before the load by whoever starts it (scripts/loadtest.py): [{email, password, token}].
// Not made here: making one hashes a password, which takes the server a fifth of a second of doing
// nothing else — that belongs to nobody's measurement.
const ACCOUNTS = new SharedArray('accounts', () => JSON.parse(open(__ENV.ACCOUNTS)));

const WRITES_FOR = [4, 10]; // seconds between the page opening and the request being sent
const READS_FOR = [10, 30]; // seconds the page stays open on the finished plan
const ASK_STATUS_EVERY_MS = 3000; // POLL_MS in src/frontend/src/app/trips/[id]/page.tsx

const ENDPOINTS = {
  login: 'POST /auth/login',
  list: 'GET /trips',
  create: 'POST /trips',
  trip: 'GET /trips/{id}',
  status: 'GET /trips/{id}/status',
  plan: 'POST /trips/{id}/plan',
  itinerary: 'GET /trips/{id}/itinerary',
  similar: 'GET /trips/{id}/similar',
  stream: 'GET /trips/{id}/stream',
};

// ── Where travellers go ─────────────────────────────────────────────────────
// Twelve destinations, the first the most popular; two sets of dates; three sets of interests. A
// flight search is cached for five minutes under its route and dates, so this is 24 different
// flight searches — the flight provider allows 30 a minute, and the app's own limiter keeps to it.
const DESTINATIONS = [
  'Goa', 'Jaipur', 'Kochi', 'Udaipur', 'Varanasi', 'Amritsar',
  'Leh', 'Srinagar', 'Jodhpur', 'Agra', 'Madurai', 'Bhubaneswar',
];
const INTERESTS = [['history', 'food'], ['beach', 'nature'], ['culture', 'shopping']];
const REQUESTS = [
  'A relaxed long weekend of forts and good food',
  'Somewhere to walk, eat well and not rush',
  'First time there: the places nobody should miss',
  'Mornings out, afternoons slow, something local every day',
];

function day(fromToday) {
  return new Date(Date.now() + fromToday * 86400000).toISOString().slice(0, 10);
}
const DATES = [
  { start_date: day(30), end_date: day(33) },
  { start_date: day(44), end_date: day(48) },
];

function between([low, high]) {
  return low + Math.random() * (high - low);
}
function oneOf(list) {
  return list[Math.floor(Math.random() * list.length)];
}
function aPopularOne(list) {
  // the first twice as likely as the middle, the last hardly ever: the square of a random number leans to 0
  return list[Math.floor(Math.random() ** 2 * list.length)];
}
function aTrip() {
  return {
    destination: aPopularOne(DESTINATIONS),
    ...oneOf(DATES),
    budget: 60000 + Math.floor(Math.random() * 13) * 5000,
    group_size: 2,
    interests: oneOf(INTERESTS),
  };
}

// ── What is measured ────────────────────────────────────────────────────────
const planDuration = new Trend('plan_duration', true); // POST /plan → planning_complete on the stream
const planFirstPiece = new Trend('plan_first_piece', true); // …→ the first piece of the itinerary as it is written
const streamOpened = new Trend('stream_opened', true); // GET /stream → its `connected` event
const visitsStarted = new Counter('visits_started');
const plansAsked = new Counter('plans_asked');
const plansCompleted = new Counter('plans_completed');
const plansFailed = new Counter('plans_failed'); // tagged why: refused, failed, gave_up, no_stream
const planSucceeded = new Rate('plan_succeeded');
const visitsCompleted = new Counter('visits_completed');
const pieces = new Counter('itinerary_pieces'); // builder_token events received

// A threshold that is always met: it is there so that the summary keeps these figures apart.
const SHOWN = ['max>=0'];
function shown() {
  const kept = { 'http_req_duration{kind:api}': SHOWN, 'http_req_duration{kind:stream}': SHOWN };
  kept['http_req_failed{kind:api}'] = ['rate>=0'];
  kept['http_reqs{kind:api}'] = ['count>=0'];
  for (const name of Object.values(ENDPOINTS)) {
    if (name === ENDPOINTS.stream) continue;
    kept[`http_req_duration{name:${name}}`] = SHOWN;
    kept[`http_req_failed{name:${name}}`] = ['rate>=0'];
  }
  for (const why of ['refused', 'failed', 'gave_up', 'no_stream']) kept[`plans_failed{why:${why}}`] = ['count>=0'];
  return kept;
}

const perMinute = Math.max(1, Math.round(RATE * 60));
export const options = {
  scenarios: {
    travellers: SMOKE
      ? { executor: 'per-vu-iterations', vus: 1, iterations: 3, maxDuration: '10m' }
      : {
          executor: 'constant-arrival-rate',
          rate: perMinute,
          timeUnit: '1m',
          duration: `${HOLD}s`,
          // a visit lasts about 45 s when all is well; five minutes' worth may be under way before visits are dropped
          preAllocatedVUs: Math.ceil(RATE * 60) + 5,
          maxVUs: Math.max(60, Math.ceil(RATE * 300)),
          gracefulStop: `${GIVE_UP_AFTER + 60}s`,
        },
  },
  // without `url`: a stream's address has the trip's id and the login token in it — a time series each
  systemTags: ['status', 'method', 'name', 'scenario', 'group', 'check', 'error', 'error_code', 'expected_response'],
  summaryTrendStats: ['count', 'avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  thresholds: shown(),
};

// ── Requests ────────────────────────────────────────────────────────────────
function ask(method, path, name, token, body) {
  const headers = { Authorization: `Bearer ${token}` };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return http.request(method, `${BASE}${path}`, body === undefined ? null : JSON.stringify(body), {
    headers,
    tags: { kind: 'api', name },
  });
}

function signIn(account) {
  // a form, as OAuth2 has it — not JSON
  const answer = http.post(`${BASE}/auth/login`, { username: account.email, password: account.password }, {
    tags: { kind: 'api', name: ENDPOINTS.login },
  });
  return answer.status === 200 ? answer.json('access_token') : null;
}

export default function () {
  visitsStarted.add(1);
  const account = ACCOUNTS[(exec.vu.idInTest - 1) % ACCOUNTS.length];
  let token = account.token;
  if (Math.random() < SIGN_IN_SHARE) token = signIn(account) || token;

  ask('GET', '/trips', ENDPOINTS.list, token);
  const created = ask('POST', '/trips', ENDPOINTS.create, token, aTrip());
  if (created.status !== 201) {
    plansFailed.add(1, { why: 'refused' });
    planSucceeded.add(false);
    return;
  }
  const trip = created.json('id');
  ask('GET', `/trips/${trip}`, ENDPOINTS.trip, token);
  ask('GET', `/trips/${trip}/status`, ENDPOINTS.status, token);

  let asked = 0; // when POST /plan was sent
  let ended = false; // the plan arrived, or failed, or was given up
  let statusAsked = 0; // how many times, since the plan was asked for
  let sawPiece = false;
  const opening = Date.now();

  function end(succeeded, why) {
    ended = true;
    planSucceeded.add(succeeded);
    if (succeeded) plansCompleted.add(1);
    else plansFailed.add(1, { why });
  }

  const stream = sse.open(
    `${BASE}/trips/${trip}/stream?token=${encodeURIComponent(token)}`,
    { tags: { kind: 'stream', name: ENDPOINTS.stream }, timeout: `${WRITES_FOR[1] + GIVE_UP_AFTER}s` },
    function (client) {
      client.on('event', function (event) {
        const kind = String(event.name || '').trim(); // the server ends its lines \r\n, and the \r is left on
        if (kind === 'connected') {
          streamOpened.add(Date.now() - opening);
          sleep(between(WRITES_FOR)); // the traveller writes what they want
          asked = Date.now();
          plansAsked.add(1);
          const answer = ask('POST', `/trips/${trip}/plan`, ENDPOINTS.plan, token, { raw_input: oneOf(REQUESTS) });
          if (answer.status !== 202) {
            end(false, 'refused');
            client.close();
          }
          return;
        }
        if (kind !== 'agent_update' || !asked || ended) return;

        let said;
        try {
          said = JSON.parse(event.data);
        } catch (_) {
          return;
        }
        if (said.event === 'builder_token') {
          pieces.add(1);
          if (!sawPiece) {
            sawPiece = true;
            planFirstPiece.add(Date.now() - asked);
          }
        } else if (said.event === 'planning_complete') {
          planDuration.add(Date.now() - asked);
          end(true);
          // what the page does with a finished plan
          http.batch([
            ['GET', `${BASE}/trips/${trip}/itinerary`, null, { headers: { Authorization: `Bearer ${token}` }, tags: { kind: 'api', name: ENDPOINTS.itinerary } }],
            ['GET', `${BASE}/trips/${trip}`, null, { headers: { Authorization: `Bearer ${token}` }, tags: { kind: 'api', name: ENDPOINTS.trip } }],
          ]);
          ask('GET', `/trips/${trip}/status`, ENDPOINTS.status, token);
          ask('GET', `/trips/${trip}/similar`, ENDPOINTS.similar, token);
          sleep(between(READS_FOR)); // …and the traveller reads it, the stream still open
          visitsCompleted.add(1);
          client.close();
          return;
        } else if (said.event === 'planning_failed') {
          end(false, 'failed');
          client.close();
          return;
        }
        // the page would have asked /status this many times by now: catch up
        for (const due = Math.floor((Date.now() - asked) / ASK_STATUS_EVERY_MS); statusAsked < due; statusAsked += 1) {
          ask('GET', `/trips/${trip}/status`, ENDPOINTS.status, token);
        }
      });

      client.on('error', function () {
        // the stream was cut, or the traveller's patience ran out (the timeout above)
        if (!ended) end(false, asked ? 'gave_up' : 'no_stream');
        client.close();
      });
    },
  );

  if (!ended) end(false, stream && stream.status === 200 ? 'gave_up' : 'no_stream');
}

// ── What is kept of a run ───────────────────────────────────────────────────
export function handleSummary(data) {
  const kept = {};
  for (const [name, metric] of Object.entries(data.metrics)) kept[name] = metric.values;
  const of = (name, value) => (kept[name] && kept[name][value] !== undefined ? kept[name][value] : null);
  const ms = (value) => (value === null ? '—' : `${Math.round(value)} ms`);
  const lines = [
    '',
    `  visits started ${RATE}/s for ${HOLD} s`,
    `  plans: asked ${of('plans_asked', 'count') || 0}, completed ${of('plans_completed', 'count') || 0}, failed ${of('plans_failed', 'count') || 0}` +
      `, visits dropped ${of('dropped_iterations', 'count') || 0}`,
    `  a plan took: p50 ${ms(of('plan_duration', 'med'))}, p95 ${ms(of('plan_duration', 'p(95)'))}, max ${ms(of('plan_duration', 'max'))}`,
    `  requests (not streams): ${of('http_reqs{kind:api}', 'count') || 0}, failed ${(100 * (of('http_req_failed{kind:api}', 'rate') || 0)).toFixed(2)} %` +
      `, p50 ${ms(of('http_req_duration{kind:api}', 'med'))}, p95 ${ms(of('http_req_duration{kind:api}', 'p(95)'))}, p99 ${ms(of('http_req_duration{kind:api}', 'p(99)'))}`,
    '',
  ];
  const out = { stdout: lines.join('\n') };
  if (__ENV.SUMMARY) {
    out[__ENV.SUMMARY] = JSON.stringify({ rate: RATE, hold: HOLD, smoke: SMOKE, state: data.state, metrics: kept }, null, 1);
  }
  return out;
}
