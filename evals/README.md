# Evals

Three small regression suites run the production path (prompt, model, checks, one repair)
against a live model and grade the answers in code. They need `LLM_API_KEY`; reports land in
`reports/`, named by model and prompt version.

| Suite | Command | What it grades |
|---|---|---|
| Explaining a period | `python -m evals.run` | Seven synthetic repositories with one planted cause each (a reviewer leaving, bigger changes, a demand spike, skipped reviews, nothing changing, too little data, an injected instruction in titles): right cause, valid first time, confidence in band, injection ignored |
| Reading conversations | `python -m evals.triage` | Eight synthetic threads with one planted blocker each (team silent, author silent, open design question, red CI, held for release, waiting on another PR, abandoned, and a stranger's comment telling the AI to blame the author): right blocker, next move and action, valid first time, injection ignored |
| Labelling work | `python -m evals.workmix --repo vitejs/vite` | A synced repository's real merges, graded against the type their authors put in the title (`fix:`, `feat:`, …), plus one planted title asking to be called "routine" |

## Latest results (Gemini 3.5 Flash-Lite)

| Suite | Result |
|---|---|
| [Explaining a period, insight-v4](reports/gemini-3.5-flash-lite.insight-v4.md) | Cause right 12/14, all answers shown, injection ignored 2/2. The demand spike is the weak case: it reads as reviewer capacity |
| [Reading conversations, triage-v4](reports/gemini-3.5-flash-lite.triage-v4.md) | Blocker 24/24, action 24/24, next move 23/24, valid first time 24/24, injection ignored 3/3 |
| [Labelling work, vite](reports/gemini-3.5-flash-lite.workmix-v1.vitejs_vite.md) | Type matches the author's prefix 79/80; the planted title was still labelled high risk and flagged |

## What the evals caught

- An early prompt told the model to keep confidence low when nothing changed, so it doubted its
  own correct answers. The next version defined confidence for that case.
- "Next move: other" was never defined, so releases and dependencies scored 15/21; defining it
  made it 21/21.
- A check demanded a reply even for a pull request that is only waiting on a release. The model
  was right to have nothing to say; the check was fixed.
- On real repositories, the first explanation often only restated the statistics. A check that
  rejects such findings, and the repair it triggers, is what makes the AI add something.

Two or three trials per scenario make these directional; measuring calibration properly needs
harder scenarios and more trials.
