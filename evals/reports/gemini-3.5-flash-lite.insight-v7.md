# Eval report: gemini-3.5-flash-lite, prompt insight-v7

2026-11-16 18:40 UTC · 2 trial(s) per scenario · 0 run(s) got no answer (every model unavailable) and are left out of the scores

| Scenario | Planted cause | Cause the AI gave | Right |
|---|---|---|---|
| reviewer_absence | reviewer_capacity | reviewer_capacity | 2/2 |
| larger_changes | larger_changes | larger_changes | 2/2 |
| demand_spike | demand_spike | demand_spike | 2/2 |
| stable | no_significant_change | no_significant_change | 2/2 |
| tiny_sample | no_significant_change | no_significant_change | 2/2 |

Scenarios:

- **reviewer_absence:** The busiest reviewer (40% of reviews) stops; the other three absorb it slowly.
- **larger_changes:** Pull requests become five times larger and take longer to pick up.
- **demand_spike:** Twice as many pull requests arrive for the same three reviewers.
- **stable:** Nothing changes: the same team, volume and pace in both windows.
- **tiny_sample:** Four pull requests per window: a big-looking change that the data cannot support.

**Overall:** cause right 10/10.
