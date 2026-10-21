# Seatlock load-test benchmarks

Runs recorded by `loadtest/verify.py`. Each entry includes the load generator command and machine details, plus database invariant results.

## 2026-10-18T14:51:20.859922+00:00 — show 16

- Hardware: Darwin 25.5.0, arm64, 8 logical CPUs, 8.0 GiB RAM; Mac Apple Silicon 8 logical CPUs/8 GiB RAM; k6 capped at GOMAXPROCS=2; Docker services limited to 4 CPUs/3.8 GiB
- k6 command: `GOMAXPROCS=2 k6 run --summary-export=loadtest/k6-summary-16.json --env BASE_URL=http://127.0.0.1:8000 --env SHOW_ID=16 --env VUS=10000 --env PAYMENT_WEBHOOK_SECRET=<configured> loadtest/flash_sale.js`
- Results: latency and throughput were unavailable because the initial summary parser did not handle k6’s exported metric shape.
- Sold: 481/500; double bookings: 0. This pre-fix run also exposed 25 bookings without payment/hold evidence, leading to atomic pending payment-intent creation during booking confirmation.

## 2026-10-18T14:54:45.257679+00:00 — show 18

- Hardware: Darwin 25.5.0, arm64, 8 logical CPUs, 8.0 GiB RAM; Mac Apple Silicon 8 logical CPUs/8 GiB RAM; k6 capped at GOMAXPROCS=2; Docker services limited to 4 CPUs/3.8 GiB
- k6 command: `GOMAXPROCS=2 k6 run --summary-export=loadtest/k6-summary-18.json --env BASE_URL=http://127.0.0.1:8000 --env SHOW_ID=18 --env VUS=10000 --env PAYMENT_WEBHOOK_SECRET=<configured> loadtest/flash_sale.js`
- Results: p50 2235.215 ms, p95 30091.885700000003 ms, p99 30790.795039999997 ms, throughput 495.3962634089075 req/s.
- Sold: 500/500; double bookings: 0; all confirmed bookings had payment evidence. k6 completed all 10,000 VU iterations, with 66.14% failed HTTP requests and 3,813 incomplete booking flows under local loopback load.

## 2026-10-18T14:59:53.670956+00:00 — show 20

- Hardware: Darwin 25.5.0, arm64, 8 logical CPUs, 8.0 GiB RAM; k6 GOMAXPROCS=2; local services run in Docker Desktop
- k6 command: `GOMAXPROCS=2 k6 run --summary-export=loadtest/k6-summary-20.json --env BASE_URL=http://127.0.0.1:8000 --env SHOW_ID=20 --env VUS=10000 --env PAYMENT_WEBHOOK_SECRET=<configured> loadtest/flash_sale.js`
- Results: p50 3556.7690000000002 ms, p95 30039.79025 ms, p99 30879.70354 ms, throughput 469.91739885220113 req/s.
- Sold: 500/500; double bookings: 0. The verifier found one confirmed booking without payment evidence; this run was made against the previously running API process during pool exhaustion. A clean restart and rerun follows.

## 2026-10-18T15:02:56.295349+00:00 — show 21

- Hardware: Darwin 25.5.0, arm64, 8 logical CPUs, 8.0 GiB RAM; k6 capped at GOMAXPROCS=2; Docker Desktop allocated 4 CPUs/3.8 GiB; API and generator on the same Mac
- k6 command: `GOMAXPROCS=2 k6 run --summary-export=loadtest/k6-summary-21.json --env BASE_URL=http://127.0.0.1:8000 --env SHOW_ID=21 --env VUS=10000 --env PAYMENT_WEBHOOK_SECRET=<configured> loadtest/flash_sale.js`
- Results: p50 2637.169 ms, p95 30029.97665 ms, p99 30855.06503 ms, throughput 498.8127830023502 req/s; HTTP failure rate 65.03%.
- Sold: 500/500; double bookings: 0; the verifier found no missing payment or live-hold evidence.
