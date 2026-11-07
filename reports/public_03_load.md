# Load test — public_03_load

Machine: 16 logical CPUs (win32); controller is single-process, single-threaded.

| measure | value |
|---|---|
| simulator start (history generation, not ours) | 80 s |
| controller wall clock, whole run | 510 s |
| wall clock per simulated day (includes the simulator's own advance time) | 64, 83, 81, 85, 92 s (max 92 s; SRE budget 600 s) |
| events ingested | 9,737,595 |
| ingest throughput (parse + aggregate + commit) | 80,209 events/s |
| planning time, total | 73 s |
| max ingest lag (sim minutes behind the newest chunk) | 660 |
| decision lag | ≤ 1 tick (15 sim-min): every tick ingests all new chunks before deciding |
| peak RSS (controller) | 634 MB |
| store size (SQLite, incl. per-call audit log of 7,433,295 calls) | 371 MB |
| proposals by channel/status | approval/applied: 48, approval/rejected: 3, auto/applied: 57 |

## Grade vs no-op baseline

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.401 | +17.5% | 0.264 | +8.0% | 0.537 | +7.3% | 52% | 410 (408) |
| arav_kyc_1 | 0.511 | +44.4% | 0.280 | +30.6% | 0.621 | +4.3% | 27% | 466 (370) |
| arav_renewal_0 | 0.482 | +20.0% | 0.286 | +8.4% | 0.597 | +9.9% | 51% | 430 (440) |
| kest_cancel_2 | 0.399 | +17.8% | 0.265 | +8.7% | 0.533 | +7.0% | 50% | 449 (411) |
| kest_kyc_1 | 0.509 | +44.3% | 0.283 | +32.3% | 0.614 | +3.4% | 21% | 498 (339) |
| kest_renewal_0 | 0.482 | +20.7% | 0.285 | +8.2% | 0.596 | +10.0% | 50% | 456 (433) |
| nort_cancel_2 | 0.395 | +16.5% | 0.262 | +8.1% | 0.531 | +6.4% | 48% | 432 (393) |
| nort_kyc_1 | 0.517 | +45.8% | 0.281 | +31.7% | 0.625 | +5.1% | 28% | 411 (323) |
| nort_renewal_0 | 0.487 | +20.3% | 0.286 | +8.5% | 0.601 | +9.8% | 54% | 488 (411) |
| tide_cancel_2 | 0.402 | +18.5% | 0.265 | +8.5% | 0.537 | +7.6% | 55% | 404 (407) |
| tide_kyc_1 | 0.508 | +43.8% | 0.281 | +31.0% | 0.617 | +4.3% | 26% | 496 (356) |
| tide_renewal_0 | 0.484 | +20.7% | 0.285 | +8.0% | 0.600 | +10.7% | 54% | 488 (438) |
| ALL | 0.465 | +27.3% | 0.277 | +15.2% | 0.585 | +7.7% | 42% | 5428 (4729) |
