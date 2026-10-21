import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';
import crypto from 'k6/crypto';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const SHOW_ID = __ENV.SHOW_ID;
const VUS = Number(__ENV.VUS || 10000);
const MAX_QUEUE_WAIT_SECONDS = Number(__ENV.MAX_QUEUE_WAIT_SECONDS || 180);

export const options = {
  scenarios: {
    flash_sale: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '60s', target: VUS },
        { duration: '60s', target: VUS },
      ],
      gracefulRampDown: '0s',
      gracefulStop: '0s',
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(50)', 'p(95)', 'p(99)'],
};

const confirmed = new Counter('seatlock_confirmed_bookings');
const soldOut = new Counter('seatlock_seat_unavailable');
const failures = new Counter('seatlock_flow_failures');

export function setup() {
  if (!SHOW_ID) {
    throw new Error('Set SHOW_ID to a queue-enabled show with 500 seats.');
  }
  const response = http.get(`${BASE_URL}/shows/${SHOW_ID}/seats`);
  if (response.status !== 200) {
    throw new Error(`Could not fetch show seats: HTTP ${response.status}`);
  }
  const seats = response.json();
  if (seats.length !== 500) {
    throw new Error(`Expected exactly 500 seats; got ${seats.length}`);
  }
  return { seatIds: seats.map((seat) => seat.id), startedAt: Date.now() };
}

function logSeatEvent(data, seat, outcome) {
  // Keep every winner and a sample of contention so the replay stays compact.
  if (outcome === 'winner' || Math.random() < 0.05) {
    console.log(`SEATLOCK_EVENT ${JSON.stringify({ t: Date.now() - data.startedAt, seat, outcome })}`);
  }
}

export default function (data) {
  if (__ITER > 0) {
    sleep(600);
    return;
  }
  const userId = `load-vu-${__VU}`;
  const userHeaders = { 'X-User-Id': userId };
  let joinResponse;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    joinResponse = http.post(
      `${BASE_URL}/shows/${SHOW_ID}/queue/join`,
      null,
      {
        headers: userHeaders,
        tags: { endpoint: 'queue_join', name: 'POST /shows/:show_id/queue/join' },
      },
    );
    if (joinResponse.status === 201) break;
    sleep(0.2 * (attempt + 1));
  }
  if (!check(joinResponse, { 'queue join admitted to queue': (r) => r.status === 201 })) {
    failures.add(1);
    return;
  }
  const token = joinResponse.json('token');
  const deadline = Date.now() + MAX_QUEUE_WAIT_SECONDS * 1000;
  let admitted = joinResponse.json('admitted');

  while (!admitted && Date.now() < deadline) {
    sleep(1);
    const status = http.get(
      `${BASE_URL}/shows/${SHOW_ID}/queue/status?token=${encodeURIComponent(token)}`,
      {
        headers: userHeaders,
        tags: { endpoint: 'queue_status', name: 'GET /shows/:show_id/queue/status' },
      },
    );
    if (status.status !== 200) {
      failures.add(1);
      return;
    }
    admitted = status.json('admitted');
  }
  if (!admitted) {
    failures.add(1);
    return;
  }

  const admittedHeaders = {
    ...userHeaders,
    'X-Admission-Token': token,
    'Content-Type': 'application/json',
  };
  const triedSeats = new Set();
  let seatId;
  let hold;
  for (let attempt = 0; attempt < Math.min(5, data.seatIds.length); attempt += 1) {
    do {
      seatId = data.seatIds[Math.floor(Math.random() * data.seatIds.length)];
    } while (triedSeats.has(seatId) && triedSeats.size < data.seatIds.length);
    triedSeats.add(seatId);
    hold = http.post(
      `${BASE_URL}/shows/${SHOW_ID}/seats/${seatId}/hold`,
      null,
      {
        headers: admittedHeaders,
        tags: { endpoint: 'hold', name: 'POST /shows/:show_id/seats/:seat_id/hold' },
      },
    );
    if (hold.status === 201) {
      logSeatEvent(data, seatId, 'winner');
      break;
    }
    if (hold.status === 409) {
      soldOut.add(1);
      logSeatEvent(data, seatId, 'rejected');
      continue;
    }
    if (hold.status === 429) {
      sleep(1);
      attempt -= 1;
      continue;
    }
    failures.add(1);
    return;
  }
  if (!hold || hold.status !== 201) return;

  const confirm = http.post(
    `${BASE_URL}/bookings/confirm`,
    JSON.stringify({ show_id: Number(SHOW_ID), seat_ids: [seatId] }),
    {
      headers: admittedHeaders,
      tags: { endpoint: 'confirm', name: 'POST /bookings/confirm' },
    },
  );
  if (confirm.status !== 201) {
    failures.add(1);
    return;
  }
  const bookingId = confirm.json('0.id');
  const intent = http.post(
    `${BASE_URL}/bookings/${bookingId}/pay`,
    null,
    {
      headers: admittedHeaders,
      tags: { endpoint: 'pay', name: 'POST /bookings/:booking_id/pay' },
    },
  );
  if (intent.status !== 201) {
    failures.add(1);
    return;
  }

  const eventBody = JSON.stringify({
    provider_event_id: `load-vu-${__VU}-booking-${bookingId}`,
    booking_id: Number(bookingId),
    status: 'success',
  });
  const webhookHeaders = {
    'X-Payment-Signature': crypto.hmac(
      'sha256',
      __ENV.PAYMENT_WEBHOOK_SECRET,
      eventBody,
      'hex',
    ),
    'Content-Type': 'application/json',
  };
  let webhook;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    webhook = http.post(`${BASE_URL}/webhooks/payment`, eventBody, {
      headers: webhookHeaders,
      tags: { endpoint: 'payment_webhook', name: 'POST /webhooks/payment' },
    });
    if (webhook.status === 200) break;
    sleep(0.1 * (attempt + 1));
  }
  if (webhook && webhook.status === 200) {
    confirmed.add(1);
  } else {
    failures.add(1);
  }
}
