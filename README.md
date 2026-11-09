# Flowsight

Where pull requests wait, and what to do about it.

Flowsight reads a public GitHub repository's pull requests and reviews, measures where work waits
in review, and compares a period with the one before. Only changes that are large and
statistically real are flagged. Then an LLM does what statistics can't, under one rule: **code
counts, the AI reads and writes.**

- **Why the numbers changed:** one explanation per real change, a likely root cause with a
  confidence level, and how to verify it.
- **What to do this week:** it reads the longest-waiting pull requests and ranks them by value,
  each with what is in its way, an owner and a reply ready to paste on GitHub.
- **Where the work goes:** it labels every merge by area and risk, so code can show which areas
  slowed down and which risky changes were merged without review.
- **A team update** to paste into a chat.

Every quote is checked verbatim against GitHub, and every number, person and pull request against
the data, before anything is shown. [NOTES.md](NOTES.md) has the architecture, decisions,
limitations, next steps and how AI was used to build it.

## Quickstart

You need Python 3.12+, a GitHub token that can read public repositories, and, for the AI parts, a
free [Google AI Studio key](https://aistudio.google.com/apikey) (no credit card).

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env     # set GITHUB_TOKEN and LLM_API_KEY
flowsight serve          # http://localhost:8080
```

With the GitHub CLI, `GITHUB_TOKEN=$(gh auth token) flowsight serve` works without editing `.env`.
Open the page, pick a repository and press **Investigate with AI**. The first look at a repository
syncs it from GitHub (under two minutes) and the first investigation takes 30 to 60 seconds; after
that everything is cached and loads in milliseconds.

## API

| Endpoint | Returns |
|---|---|
| `GET /v1/repos/{owner}/{name}/flow` | Metrics for the period and the one before, and the signals |
| `GET /v1/repos/{owner}/{name}/triage` | The longest-waiting pull requests: value, what is in the way, action, owner, reply, verified quotes |
| `GET /v1/repos/{owner}/{name}/work-mix` | Merges labelled by type, area and risk; waits by area; risky merges without review |
| `GET /v1/repos/{owner}/{name}/insights` | Why each real change happened, root cause, confidence, plan, and the evidence cited |
| `GET /v1/repos/{owner}/{name}/digest` | A team update, as JSON and Markdown |
| `GET /v1/repos/{owner}/{name}/sync` | How far stored data reaches, and the latest sync's state |

Each takes `from` and `to` (ISO 8601; default: the last 28 days; at most 90).

```sh
curl -s localhost:8080/v1/repos/withastro/astro/flow | jq '.signals[] | select(.status == "detected")'
curl -s localhost:8080/v1/repos/vitejs/vite/work-mix | jq '.work_mix.second_look'
curl -s localhost:8080/v1/repos/astral-sh/ruff/insights | jq '{headline, changes}'
```

- **First request for a repository:** `202 Accepted` with `Location` and `Retry-After` while it
  syncs in the background. Data older than 10 minutes is served while a refresh runs.
- **Caching:** `ETag` and `304 Not Modified`; AI answers are cached in SQLite.
- **Errors** are RFC 9457 `application/problem+json`: `400` bad input, `403` repository not
  allowed, `404` unknown or private repository, `429` AI rate limit (20 a minute per client),
  `502` GitHub failed, `503` GitHub rate limit or no LLM key. If the model is down, the backups
  are tried, then `/insights` falls back to a plain summary marked `"kind": "fallback"`.

## What is measured, and why

The question behind every metric is **where does work wait?** Each merged pull request is split
into stages: ready for review → first review → first approval → merge. Time starts at *ready for
review*, so drafts don't count; bots and authors reviewing their own work don't count as reviews;
durations are medians because review times are skewed. The period also reports pull requests
**still waiting for a first review** at its end, the queue that medians of merged work can't see.

A change becomes a **signal** only if both periods have at least 10 samples, it is large enough to
matter (25% and an hour, or 10 points for rates), and it is significant after a false-discovery
correction across all metrics (Mann–Whitney, Fisher's exact or binomial tests, Benjamini–Hochberg
q < 0.05). Without that, a dashboard flags several "changes" a week by chance, and an LLM happily
explains each one.

## How the AI is kept honest

1. **It sees facts, not raw data:** numbered evidence (tested signals, context, readings), with
   text written by strangers truncated, stripped of hidden HTML comments and marked untrusted.
2. **Answers are schema-constrained**, with fixed lists of causes, blockers, actions and types.
3. **Code checks every answer:** quotes verbatim, owners from the team or the thread, every
   number, person and pull request present in its input, every claim citing evidence that exists,
   and one explanation for each real change.
4. **One repair, then a fallback.** A reading that keeps failing is left out; an explanation
   falls back to a plain summary. An unchecked answer is never shown.
5. **Confidence is bounded and shown as a level.** It is the lower of the model's number and a
   ceiling from the statistics it cites, shown as low, moderate or high with its reason.

Three eval suites measure this against a live model; see [evals/README.md](evals/README.md).

## Configuration

Settings come from environment variables or `.env`, validated at startup; secrets are never
logged.

| Variable | Default | |
|---|---|---|
| `GITHUB_TOKEN` | *(required)* | Read-only token for public repositories |
| `LLM_API_KEY` | unset | Enables the AI endpoints (they return 503 without it) |
| `LLM_BASE_URL` | Gemini's OpenAI-compatible endpoint | Any OpenAI-compatible API, e.g. Ollama at `http://localhost:11434/v1` |
| `LLM_MODEL` | `gemini-3.5-flash-lite` | |
| `LLM_BACKUP_MODELS` | `gemini-3.5-flash` | Tried in order when a model is rate limited or down |
| `ALLOWED_REPOS` | any public repository | Comma-separated `owner/name` allow-list |
| `HOST`, `PORT` | `127.0.0.1`, `8080` | |
| `DATABASE_PATH` | `data/flowsight.db` | SQLite, created on first run |

## Development

```sh
pip install -e '.[dev]'
pytest                                       # 162 tests, no network or keys needed
ruff check . && ruff format --check . && mypy src tests evals
python -m evals.run && python -m evals.triage   # evals against a live model
cd web && npm ci && npm run build            # only when changing the UI (web/src/app.ts)
```

```
src/flowsight/
  github/      GraphQL client (retries, rate limits) and queries
  store.py     SQLite: pull requests, reviews, sync coverage, cached AI answers
  sync.py      incremental sync, run in the background by api/sync_jobs.py
  analytics/   per-PR stages, period metrics, significance tests → signals
  triage/      which pull requests to read → case files → LLM → verified readings
  workmix/     areas → labels → checks → counts by type and area
  insights/    evidence → LLM → validation → bounded confidence; the digest
  api/         FastAPI app and error responses
  web/         the page, compiled from web/src/app.ts
evals/         eval suites and their latest reports
tests/         tests against fakes of GitHub and the LLM
```
