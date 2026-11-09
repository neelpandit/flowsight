# Evals

Code checks every answer's citations, numbers, people and quotes before it is shown, so those
are always right on anything you see. What code can't check is whether the model drew the right
conclusion from the facts. This eval measures that, and only that.

`python -m evals.run` builds five made-up repositories, each slowed down for one known reason,
runs them through the real pipeline (statistics → facts → model → checks) against a live model,
and asks one question of each answer: **did the model name the planted cause?** It needs
`LLM_API_KEY`. Each run writes a markdown report and a JSON summary to `reports/`.

| Made-up repository | Planted cause |
|---|---|
| The busiest reviewer (40% of reviews) stops; the other three absorb it slowly | not enough reviewer time |
| Pull requests become five times larger and take longer to pick up | larger changes |
| Twice as many pull requests arrive for the same three reviewers | a demand spike |
| Nothing changes: the same team, volume and pace in both windows | no significant change |
| Four pull requests per window: a big-looking change the data cannot support | no significant change |

## Latest result

10 of 10 (two trials each, 2026-11-16, gemini-3.5-flash-lite with its backups). An earlier run
the same day scored 9 of 10: the miss read the demand spike as a capacity problem, which is a
near miss, since more work for the same reviewers is a capacity problem.

## What the eval caught

- An early prompt told the model to keep confidence low when nothing changed, so it doubted its
  own correct answers. The next version defined confidence for that case.
- On real repositories, the first explanation often only restated the statistics. A check that
  rejects such findings, and the repair it triggers, is what makes the AI add something.

## Limits

Five scenarios with one cause each, and one or two trials: directional, not statistical. It
catches a prompt that broke, not a one-point difference between models. Real repositories mix
causes, and the fixed list of causes makes a near miss count as a miss.
