# Intraday evaluation verification — 2026-08-22

This is a literal audit of the private local database after the strict hourly implementation and cached recomputation. No MiniMax call, Bilibili download, transcription, or media processing was used for this backfill.

## Refresh and cache

- Daily XAU/USD bars refreshed: 127.
- One-hour XAU/USD bars cached: 5,683.
- Hourly provider: `XAUS (xaus.com; Yahoo Finance proxy)`.
- Hourly UTC coverage: `2025-08-22T04:00:00+00:00` through `2026-08-21T20:00:00+00:00`.
- All latest claims after corrected intraday classification: 230 evaluated and 224 unresolved; 0 failed.

## Latest explicit intraday claims

Explicit intraday wording takes precedence over a contradictory maximum-trading-day value. The audit found 120 latest intraday claims:

| Creator UID | Total | Hit | Near | Miss | Other/unresolved |
| --- | ---: | ---: | ---: | ---: | ---: |
| `1847287889` | 33 | 18 | 1 | 6 | 8 |
| `546630884` | 87 | 35 | 6 | 42 | 4 |
| **Total** | **120** | **53** | **7** | **48** | **12** |

The 12 remaining results consist of 5 neutral primary trends, 2 qualitative-volatility claims without a numeric threshold, 1 externally checked condition that did not trigger, and 4 claims with no eligible complete post-publication hour. No unfinished numeric intraday forecast was marked as a miss (`intraday_horizon_not_mature`: 0 at audit time).

The four missing-hour results are explainable from the cached source rather than a timestamp-routing failure: three claims were published at `11:02:20 UTC` and expired at `12:30:00 UTC`, before the `12:00–13:00` hour was complete; one claim's market-data session had no cached bars before its deadline.

## Hard constraints and smoke checks

- Evaluations whose first eligible hour precedes the video publication timestamp: **0**.
- SQLite `PRAGMA integrity_check`: **ok**.
- Jobs in `pending`, `running`, or `paused`: **0**.
- Temporary `.wav`, `.m4a`, `.mp3`, or `.mp4` residue under `data/tmp`: **0**.
- Full automated suite: **294 passed**.
- `compileall`: passed.
- `git diff --check`: passed.
- Loopback routes `/`, both creator pages, both claim-result pages, and `/api/status`: HTTP 200.
- Timestamped intraday video smoke route: HTTP 200 and displays publication, first complete hour, hit, and deadline evidence.
- Secret-prefix scan across smoke-test responses: no match.
