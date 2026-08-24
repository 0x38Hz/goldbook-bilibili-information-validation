# Goldbook verification checklist

This checklist records the final local verification on 2026-08-21. Live
analysis used the configured local credential without printing or copying it;
verification retained neither media nor raw provider payloads.

| Design requirement | Status | Evidence |
| --- | --- | --- |
| 2: Public Bilibili video/space scope, independent creator research lines, 183-day default | PASS | `test_bilibili.py`, `test_config.py`, `test_jobs.py` |
| 2: Local Chinese Whisper, bounded MiniMax concurrency, XAU/USD daily data, claim-specific horizons | PASS (live + offline) | `test_transcribe.py`, `test_minimax.py`, `test_claim_extraction.py`, `test_claim_time.py`, `test_market.py` |
| 2: Ranking, compilation, detail/review, SQLite persistence, resume/retry | PASS | `test_web.py`, `test_db.py`, `test_jobs.py` |
| 2: No comments/danmaku/identity collection; no protected/private content; no trading/public deployment/other assets/intraday replay | PASS | source boundary tests, templates and CLI scans |
| 4 config: local-only host; safe `.env` loading keeps the key server-only; concurrency is bounded 1--8 | PASS | configuration/security tests and real status smoke |
| 4 database: seven entities, short transactions, idempotency, cascades, shared prices and migrations | PASS | `test_db.py` |
| 4 Bilibili: canonical public sources, 183-day filter, task temp paths, no bypass options and metadata-only storage | PASS | `test_bilibili.py`; public-only discovery completed with 40 and 59 videos for the supplied UIDs |
| 4 transcription: lazy singleton, CUDA/float16 or CPU/int8, Chinese VAD timestamped segments and retry boundary | PASS | `test_transcribe.py`; local `small` snapshot, packaged CUDA 12 cuBLAS registration, batched RTX 5080 inference, and CPU fallback verified |
| 4 MiniMax: publication-only prompt, hard eight-way limit, retry/cache/schema and per-claim evidence gate | PASS (live + automated) | `test_minimax.py`, `test_claim_extraction.py`; MiniMax-M3 completed all 98 non-empty transcripts with zero final failures |
| 4 market: Stooq adapter with strict XAUS spot fallback, sorted valid OHLC, cache and unpriced recovery | PASS | `test_market.py`, `test_scoring.py`; live fallback returned and persisted 128 daily bars for the 183-day window |
| 4 scoring: strict publication alignment, claim-specific windows, point/range/sequence/direction evaluation, video-equal creator metrics | PASS | `test_claim_time.py`, `test_claim_evaluation.py`, `test_claim_metrics.py`, `test_claim_web.py`; 193 claims all have evaluations |
| 4 jobs: single worker, pause/retry/recover/CAS, one active video job, cleanup | PASS | `test_jobs.py` |
| 5 home/creator management, delete semantics and progress/status | PASS | `test_web.py`, `test_jobs.py`, real `/` and creator smoke |
| 5 leaderboard eligibility split, five-day average default, metrics/disposition disclosure | PASS | `test_web.py::test_leaderboard_uses_five_day_average_signed_return_for_ordering` |
| 5 compilation ordering/cards/filters/public source link without embedded media | PASS | `test_web.py`, template inspection |
| 5 per-video claim comparison, optional correction revisions, recomputation and chart markers | PASS | `test_claim_web.py`; all 99 creator-scoped video pages returned HTTP 200 and all 94 claim-bearing pages rendered claim comparisons |
| 6 local-only/copyright/privacy wording, no retained media, short evidence only, deletion/correction and non-advice warning | PASS | README/template scan, `test_bilibili.py`, `test_jobs.py` |
| 6 rotate leaked key guidance | PASS | README; the configured credential remains only in ignored local `.env` and exact-value scan found no copy elsewhere |
| 7 provider/model/parse/market/DB/crash errors are safe, retryable and do not log secret/transcript/raw response | PASS (offline) | `test_jobs.py`, `test_minimax.py`, `test_market.py`, `test_db.py` |
| 8 required unit/integration coverage and manual safety acceptance | PASS (automated/local) | final `.venv` pytest: 191 passed; compileall and pip check passed; 104-route loopback smoke; media/secret scans |
| 9 source, tests, pinned requirements, example environment, setup/start scripts and offline demo | PASS | `test_cli.py`, `test_demo.py`, README |
| 9 default local database, temporary audio cleanup and ignored local artifacts | PASS | `.gitignore`, `test_jobs.py`, final media scan |

## Local visual run

To view the isolated fictional demo without using Bilibili, MiniMax, Stooq or
Whisper, run:

```powershell
python -m goldbook seed-demo
python -m goldbook serve --data-dir .\data\demo --database-name goldbook-demo.db
```

Then open `http://127.0.0.1:8765/`.  The Task 8 smoke instead used a fresh
temporary data directory and removed it after verifying `/`, `/leaderboard`,
`/creators/demo-aurora`, `/videos/BVDEMOA1`, and `/api/status`.

## Live verification results and limitations

- MiniMax-M3 live claim analysis completed for all 98 videos with non-empty
  transcripts; one empty-audio video remains explicitly without a transcript.
  The database has 193 retained, transcript-locatable claims and 193 evaluation
  rows: 26 hit, 2 near, 25 miss, and 140 unresolved. Provider
  responses and credentials were not logged or copied into reports.
- faster-whisper, CTranslate2 and yt-dlp are installed in `.venv`. The official
  Hugging Face route timed out, so the same Systran CTranslate2 `small` model
  was obtained from ModelScope and verified from `models/faster-whisper-small`
  without Hugging Face access. Windows CUDA 12 cuBLAS and batched GPU inference
  were verified; no audio was retained after processing.
- The supplied public creators are `546630884` (指尖金汇-黄金) and
  `1847287889` (质子黄金). Public-only discovery produced 40 and 59 videos in
  the 183-day window; all 99 video jobs and both creator-sync jobs are complete.
- Current claim ranking: `1847287889` has 104 claims, 26 mature, 61.90
  video-equal score, 59.52% exact hit rate and 25.00% coverage; `546630884`
  has 89 claims, 27 mature, 43.86 video-equal score, 41.23% exact hit rate and
  30.34% coverage. Unresolved claims are excluded from wins and losses.
- Stooq currently serves an HTTP/JS challenge and Yahoo direct access returned
  403. The strict XAUS fallback provided 128 fresh upstream XAU/USD spot daily
  bars for 2026-02-19 through 2026-08-20; they were persisted locally and no
  futures substitute was accepted.
