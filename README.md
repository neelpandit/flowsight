# Flowsight

Where pull requests wait, and what to do about it.

Flowsight reads a public GitHub repository's pull requests, measures how long they wait at each
review stage, and compares the last period with the one before. A change is flagged only if it is
large and a statistical test says it is unlikely to be luck. Then an LLM explains why, and reads
the longest-waiting pull requests to say what to do this week. Code checks every number, name and
quote the model writes before it is shown.

[NOTES.md](NOTES.md) has the architecture, the decisions and why, limitations, next steps and
how AI was used to build it.

## Quickstart

Python 3.12+, a GitHub token that can read public repositories, and a free
[Google AI Studio key](https://aistudio.google.com/apikey) for the AI parts.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env     # set GITHUB_TOKEN and LLM_API_KEY
flowsight serve          # http://localhost:8080
```

Open the page, pick a repository (or type any public `owner/name`) and press **Investigate with
AI**. The first look at a repository downloads it from GitHub (a minute for most, a few for a very
busy one); after that everything is cached.

## API

| Endpoint | Returns |
|---|---|
| `GET /v1/repos/{owner}/{name}/flow` | The three wait times for the period and the one before, and which changed for real |
| `GET /v1/repos/{owner}/{name}/insights` | Why each real change happened: a narrative, a root-cause hypothesis, a confidence, and the facts each claim cites |
| `GET /v1/repos/{owner}/{name}/triage` | The longest-waiting open pull requests: why each is stuck, who should act, what to do, a reply to post |
| `GET /v1/repos/{owner}/{name}/sync` | How far stored data reaches, and the latest sync's state |

Each takes `from` and `to` (ISO 8601; default the last 28 days; at most 90). Try:

```sh
curl -s localhost:8080/v1/repos/angular/angular/flow | jq '.signals'
curl -s localhost:8080/v1/repos/angular/angular/insights | jq '{headline, hypothesis}'
```

The first request for a repository answers `202 Accepted` with `Retry-After` while it syncs.
Responses carry `ETag`s; errors are RFC 9457 `application/problem+json`; AI endpoints are rate
limited (`429`) and answer `503` without an LLM key.

## What is measured, and why

The question is **where does work wait?** Each merged pull request is split into three stages:
ready for review → first review → first approval → merge. The clock starts at *ready for review*,
so drafts don't count; bots and self-reviews don't count; each stage is reported as a median,
because waits are skewed. The page also shows how many pull requests were still waiting for a
first review at the end of the period.

A stage becomes a **real change** only if both periods have at least 10 pull requests, the median
moved by at least 25% and an hour, and a Mann–Whitney rank test, corrected for testing three
stages at once (Benjamini–Hochberg, q < 0.05), says the shift is unlikely to be luck. The code
and the reasons are in `src/flowsight/analytics/signals.py`.

## How the AI is kept honest

- **It sees facts, not raw data.** The explanation gets about 18 numbered facts, all numbers:
  the three stages and context such as volume, reviewers and the queue. The readings get one
  conversation each, marked as untrusted text.
- **Answers are structured**: a fixed schema with fixed lists of causes, blockers and actions,
  enforced by the API.
- **Code checks every answer**: cited fact IDs exist, every number and person is in the input,
  quotes are verbatim, every real change gets an explanation. One repair, then a plain fallback.
- **Confidence is capped** by how well the hypothesis fits the statistics, never above 0.85,
  and shown as low, moderate or high.
- **One eval** measures what code can't check: five made-up repositories, each slowed down for one
  known reason; did the model name it? Latest run 10 of 10. See [evals/README.md](evals/README.md).

## Configuration

| Variable | Default | |
|---|---|---|
| `GITHUB_TOKEN` | *(required)* | Read-only token for public repositories |
| `LLM_API_KEY` | unset | Enables the AI endpoints |
| `LLM_BASE_URL` | Gemini's OpenAI-compatible endpoint | Any OpenAI-compatible API, e.g. Ollama |
| `LLM_MODEL` | `gemini-3.5-flash-lite` | |
| `LLM_BACKUP_MODELS` | `gemini-3.1-flash-lite, gemini-3-flash-preview, gemini-3.5-flash` | Tried in order when a model is down |
| `ALLOWED_REPOS` | any public repository | Comma-separated allow-list |
| `HOST`, `PORT` | `127.0.0.1`, `8080` | |
| `DATABASE_PATH` | `data/flowsight.db` | SQLite, created on first run |

## Development

```sh
pip install -e '.[dev]'
pytest                                       # 139 tests, no network or keys needed
ruff check . && ruff format --check . && mypy src tests evals
python -m evals.run                          # the eval, against a live model
cd web && npm ci && npm run build            # only when changing the page (web/src/app.ts)
```

```
src/flowsight/
  github/      GraphQL client and queries
  store.py     SQLite: pull requests, reviews, cached AI answers
  sync.py      incremental sync, run in the background
  analytics/   stages → medians → Mann–Whitney → real changes
  triage/      pick stuck pull requests → case files → LLM → checks
  insights/    facts → LLM → checks → capped confidence
  api/         FastAPI app
  web/         the page, compiled from web/src/app.ts
evals/         the eval and its latest report
tests/         tests against fakes of GitHub and the LLM
```
