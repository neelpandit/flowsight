# Notes

## How to run it locally

Prerequisites: Python 3.12+, a GitHub token that can read public repositories, and a free
[Google AI Studio key](https://aistudio.google.com/apikey) for the AI parts. Node is only needed
to change the UI.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env     # set GITHUB_TOKEN and LLM_API_KEY
flowsight serve          # http://localhost:8080
pytest                   # 139 tests, no network or keys needed
```

The README lists the environment variables, the API and the metric definitions.

## Architecture tour

Flowsight is one Python process (FastAPI) with SQLite beside it. A request for a repository goes
to a **sync coordinator** first: if the stored data covers the period and its baseline, the
answer comes from SQLite; if not, a background sync starts and the caller gets `202 Accepted`
with somewhere to poll, since a first sync of a busy repository takes minutes (about four for next.js). The
sync uses GitHub's GraphQL API in two phases (list pull requests by `updatedAt`, then fetch only
the changed ones in batches, with reviews and draft history), so refreshes are incremental and
stale data keeps being served meanwhile. **Analytics** split each merged pull request into the
stages where it can wait, compare the period with the one before, and promote a change in a
wait time to a **signal** only when it is large and survives a rank test with a correction for
testing several metrics. That is `/flow`, endpoint one.

The AI part follows one rule: **code counts, the AI reads and writes.** `/triage` turns the
conversations of the longest-waiting pull requests into case files (facts from code, the thread
labelled by who spoke) and gets back, per pull request, what is in its way, an action, an
owner and a reply to post. `/insights` gives the model only numbers: the signals, and context
such as volume, reviewer load and counts over those readings (how many are waiting on the
team), and asks why each real change happened, the likely root cause and a confidence. The model is treated as untrusted:
strangers' text is truncated and marked, answers are schema-constrained, and code checks every
quote, person, number and citation before anything is shown, allowing one repair before falling
back. Answers are cached, an eval measures accuracy against a live model on
data with a known answer, and a small TypeScript page puts the AI investigation first.

## Decisions, and why

- **Where pull requests wait, not who did the most.** Leaderboards reward volume and say little
  a team can act on. Stage times say where work queues, and they have causes worth explaining.
- **Statistics before the LLM.** Asked to "find what's interesting", a model explains noise with
  confidence. Code decides what is real; the model only explains changes that passed.
- **Only wait times are tested.** The question is where work waits, so merges, merges without
  review and who reviews are context for the model, never "changes" in their own right. That
  also leaves one test, Mann–Whitney, chosen because waits are very uneven: most take minutes,
  a few take weeks, and a rank test is not thrown by those.
- **Code counts, the AI reads and writes.** The first version gave the model only statistics,
  and it read the dashboard back. The second had it report "nobody responded", which a query
  already knew. Now code computes every fact it can, and the model does what needs reading or
  writing: what is in a pull request's way, who should act, and the words of a reply.
- **Every real change gets its own explanation**, enforced by validation, so the numbers and
  the AI can't drift apart.
- **Confidence as a level.** The number was always one of a few round values (the model's own,
  capped by a step from a table), so the page shows low, moderate or high with its reason.
- **Drafts, never posting.** Replies and the update have copy buttons; the token is read-only.
- **Any OpenAI-compatible LLM, Gemini's free tier by default**, behind a one-method interface
  that tests and evals fake. At Tempo this would be Claude on Bedrock, with the same checks.
- **SQLite and a background sync answering 202**, rather than Postgres or a two-minute request.
- **Python, with TypeScript only for the page:** one file compiled by `tsc`, no framework.

## Things I chose not to do

- **Authentication:** it binds to `127.0.0.1`; `ALLOWED_REPOS` limits what it fetches.
- **Docker:** one process and a SQLite file, so `pip install` is shorter.
- **Private repositories:** refused, so no one's private data passes through a shared token.
- **An MCP server or an agent that chooses what to read:** worth adding, but a fixed pipeline is
  cheaper and easier to evaluate first.

## Known limitations

- **Number checks are a heuristic.** Percentages, differences and multiples of evidence numbers
  pass, so a small integer can match by chance, and words like "doubled" aren't checked.
- **"Team" is inferred.** Readings treat people who approved two or more merged pull requests
  by others as the team, since GitHub's association hides private org membership. A new
  maintainer with one approval still reads as community.
- **Readings cover the 15 longest waits**, so a cause hidden in the 16th is missed.
- **Bot-like user accounts** (release bots) count as human authors.
- **The confidence cap is a judgement**, and the eval suites are too small to
  measure calibration properly.
- **The free tier has a daily quota.** Backup models, caching and a plain fallback cover it.

## Correctness check

Merged pull request counts for 2026-10-11 to 2026-11-08 match GitHub's own search
(`is:pr is:merged merged:<range>`) exactly: astro 161, vite 100, ruff 465.

## What I'd do with another day

1. **Claude on Bedrock** behind the same `InsightModel` interface, then run the eval
   on it and compare with Gemini Flash-Lite on accuracy, cost and latency.
2. **A split by team.** Inside a company the wait that matters most is often on pull requests
   from another team or from contractors. Split the first-review wait by owning team (from
   CODEOWNERS or team membership) and test it the same way; the open-source version of that
   split (maintainers vs outside contributors) was tried and dropped, as GitHub's author
   association means nothing inside an org.
3. **Evals in CI on prompt changes**, with harder scenarios that have a tempting wrong answer, so
   calibration can be measured properly rather than with 14 trials.

Further out: an MCP server over the endpoints and an agent that can follow a hunch ("read every
pull request this reviewer was requested on"); reviewer briefings that read the diff; and Jira
as a second source, so waiting time links to tickets and logged time.

## How I used AI

**In the product.** Gemini 3.5 Flash-Lite, on the free tier through an OpenAI-compatible API,
reads conversations and explains the period. It is never
trusted on its own word: everything above about validation, repairs and evals exists for that.

**Building it.** I worked with Claude Code, Anthropic's coding assistant, as a pair programmer.
Roughly, the split was:

- **Me: the problem, the direction and the judgement.** I chose the problem (where review work
  waits, rather than leaderboards), GitHub as the source, Python with TypeScript only for the UI,
  and a zero-cost setup. I ran each version on astro, vite and ruff and judged what it produced.
  Several turns in the design came from that: the first AI answer only restated the dashboard,
  which led to reading the conversations; the readings then kept saying "nobody engaged", which
  led to "code counts, the AI reads and writes"; the confidence was always a round number, which
  led to showing a level; and the page was a wall of text, which led to the numbered sections
  that start by explaining each real change.
- **Claude Code: most of the implementation.** It drafted most of the code, tests and
  documentation to that direction, ran the spikes against the GitHub API, the eval suites, and
  the lint, type-check and test loop, and checked the page in a browser.
- **Together: the trade-offs.** Statistics before the LLM, the validation layer, caching, and
  what to leave out were discussed and decided together; I reviewed and changed the drafts.

Everything went through the same tests, ruff, mypy and CI as hand-written code would.
