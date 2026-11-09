// Flowsight UI: one page over the JSON API. No framework, no bundler.
// Every string from the API is inserted with textContent, never as HTML: pull request titles
// and comments are written by strangers.

// ---- API types (the subset the page uses) ----------------------------------------------------

type SignalStatus = "detected" | "not_detected" | "insufficient_data";

interface Period {
  start: string;
  end: string;
}

interface Distribution {
  n: number;
  p50: number;
}

interface Signal {
  id: string;
  metric: string;
  description: string;
  unit: "hours" | "pull_requests" | "ratio";
  current: number | null;
  baseline: number | null;
  status: SignalStatus;
}

interface WindowMetrics {
  merged: number;
  merged_without_review_rate: number | null;
  waiting_for_first_review_at_end: number;
  stages: { wait_for_first_review: Distribution | null };
  wait_for_first_review_by_author: { external: Distribution | null };
  reviewers: { top_share: number | null };
}

interface FlowResponse {
  repository: string;
  data_as_of: string;
  refreshing: boolean;
  period: Period;
  current: WindowMetrics;
  baseline: WindowMetrics;
  signals: Signal[];
}

interface EvidenceItem {
  id: string;
  label: string;
  unit: string;
  current: number | null;
  baseline: number | null;
  status: SignalStatus | null;
  pull_request: number | null;
  untrusted_title: string | null;
  detail: string | null;
}

interface Cited {
  evidence: string[];
}

interface InsightResponse {
  repository: string;
  generated_by: {
    kind: "llm" | "fallback";
    model: string;
    prompt_version: string;
    cached: boolean;
  };
  headline: string;
  summary: string;
  changes: (Cited & { signal: string; explanation: string })[];
  findings: (Cited & { claim: string })[];
  hypothesis: Cited & {
    statement: string;
    cause: string;
    reasoning: string;
    alternatives_considered: string[];
    how_to_verify: string;
    confidence: { level: "low" | "moderate" | "high"; basis: string };
  };
  recommended_actions: (Cited & { action: string; owner: string | null })[];
  caveats: string[];
  evidence: EvidenceItem[];
  rejected_because: string[];
}

type NextAction =
  "review_now" | "prioritise" | "decide" | "nudge_author" | "wait" | "close" | "none";

interface TriagedPullRequest {
  number: number;
  title: string;
  url: string;
  author: string | null;
  author_role: string;
  waited_days: number;
  team_responded: boolean;
  ci_checks: string | null;
  merge_conflicts: boolean;
  diagnosis: {
    summary: string;
    value: "high" | "medium" | "low";
    review_effort: "minutes" | "about_an_hour" | "deep";
    blocker: string;
    explanation: string;
    action: NextAction;
    next_step: string;
    owner: string | null;
    reply: string | null;
  };
  quotes: { text: string; who: string | null; role: string; at: string; url: string | null }[];
  events_read: number;
}

interface TriageResponse {
  models: string[];
  pull_requests: TriagedPullRequest[];
  not_shown: string[];
}

interface WorkMixResponse {
  work_mix: {
    by_type: { type: string; current: number }[];
    by_area: {
      area: string;
      description: string;
      current: number;
      median_wait_hours: number | null;
      baseline_median_wait_hours: number | null;
    }[];
    second_look: { number: number; title: string; url: string; reason: string }[];
    labelled: number;
    models: string[];
  };
}

interface DigestPoint {
  text: string;
  pull_requests: number[];
}

interface DigestResponse {
  digest: {
    title: string;
    tldr: string;
    shipped: DigestPoint[];
    needs_attention: DigestPoint[];
    decisions: DigestPoint[];
  } | null;
  markdown: string | null;
}

interface Accepted {
  retry_after_seconds: number;
  status_url: string;
}

interface SyncStatus {
  job: { status: "running" | "succeeded" | "failed" } | null;
}

class ApiError extends Error {}

// ---- small helpers ---------------------------------------------------------------------------

type Child = Node | string | null | undefined | false;

/** Creates an element. Strings become text nodes, so API data can never inject markup. */
function el(
  tag: string,
  attributes: Record<string, string> = {},
  ...children: Child[]
): HTMLElement {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    element.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return element;
}

function byId(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing #${id}`);
  return element;
}

const hours = (value: number | null | undefined): string =>
  value === null || value === undefined ? "–" : `${value.toFixed(1)} h`;
const percent = (value: number | null | undefined): string =>
  value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
const days = (value: number): string => `${Math.round(value)} days`;

function formatValue(value: number | null, unit: string): string {
  if (value === null) return "–";
  if (unit === "hours") return hours(value);
  if (unit === "ratio") return percent(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function link(href: string, text: string): HTMLElement {
  return el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Copies text to the clipboard. The page never posts anything itself. */
function copyButton(text: string, label: string): HTMLElement {
  const button = el("button", { type: "button", class: "copy" }, label);
  button.addEventListener("click", (event) => {
    event.preventDefault(); // inside a <summary>, a click would also open or close the row
    event.stopPropagation();
    void navigator.clipboard.writeText(text).then(
      () => {
        button.textContent = "Copied";
        setTimeout(() => (button.textContent = label), 1500);
      },
      () => (button.textContent = "Copy failed"),
    );
  });
  return button;
}

// ---- API calls -------------------------------------------------------------------------------

async function getJson<T>(path: string): Promise<{ status: number; body: T }> {
  const response = await fetch(path, { headers: { accept: "application/json" } });
  const body: unknown = await response.json();
  if (!response.ok && response.status !== 202) {
    const problem = body as { title?: string; detail?: string };
    throw new ApiError(problem.detail ?? problem.title ?? `Request failed (${response.status})`);
  }
  return { status: response.status, body: body as T };
}

/** Fetches a repository resource, waiting out a first sync (202 Accepted) if one starts. */
async function fetchReady<T>(path: string, onProgress: (message: string) => void): Promise<T> {
  for (let attempt = 0; attempt < 60; attempt++) {
    const { status, body } = await getJson<T | Accepted>(path);
    if (status !== 202) return body as T;
    onProgress("Fetching pull requests from GitHub for the first time… this can take a minute.");
    await waitForSync(body as Accepted);
  }
  throw new ApiError("The sync is taking unusually long; try again shortly.");
}

async function waitForSync(accepted: Accepted): Promise<void> {
  for (let attempt = 0; attempt < 60; attempt++) {
    await sleep(Math.min(accepted.retry_after_seconds, 3) * 1000);
    const { body } = await getJson<SyncStatus>(accepted.status_url);
    if (body.job?.status !== "running") return;
  }
}

// ---- state and the main flow -----------------------------------------------------------------

let current: { repository: string; query: string } | null = null;
let lastFlow: FlowResponse | null = null;

function periodQuery(numberOfDays: number): string {
  const end = new Date();
  end.setUTCMinutes(0, 0, 0);
  const start = new Date(end.getTime() - numberOfDays * 86_400_000);
  return `from=${encodeURIComponent(start.toISOString())}&to=${encodeURIComponent(end.toISOString())}`;
}

function setStatus(text: string, kind: "info" | "error" = "info"): void {
  const status = byId("status");
  status.textContent = text;
  status.className = `status ${kind}`;
  status.hidden = text === "";
}

const AI_SLOTS = ["answer", "todo", "areas", "digest"];
const INTRO = byId("ai-meta").textContent ?? "";

async function analyse(event?: Event): Promise<void> {
  event?.preventDefault();
  const slug = (byId("repo") as HTMLInputElement).value.trim();
  const numberOfDays = Number((byId("days") as HTMLSelectElement).value);
  if (!/^[\w.-]+\/[\w.-]+$/.test(slug)) {
    setStatus("Enter a repository as owner/name, e.g. withastro/astro.", "error");
    return;
  }
  const base = `/v1/repos/${slug}`;
  current = { repository: slug, query: periodQuery(numberOfDays) };
  byId("results").hidden = true;
  for (const id of AI_SLOTS) byId(id).replaceChildren();
  byId("ai-meta").textContent = INTRO;
  setStatus("Loading…");
  try {
    const flow = await fetchReady<FlowResponse>(`${base}/flow?${current.query}`, setStatus);
    current.repository = flow.repository;
    renderFlow(flow);
    setStatus("");
    byId("results").hidden = false;
  } catch (error) {
    setStatus(message(error), "error");
  }
}

// ---- the AI investigation: four steps, each shown as soon as it is done ----------------------

interface Step {
  label: string;
  slot: string;
  run: (base: string, query: string) => Promise<string>; // returns what it did, for the summary
}

const STEPS: Step[] = [
  {
    label: "Read the conversations of the longest-waiting pull requests",
    slot: "todo",
    run: async (base, query) => {
      const triage = await fetchReady<TriageResponse>(`${base}/triage?${query}`, setStepNote);
      renderTodo(triage);
      return `read ${triage.pull_requests.length} conversations`;
    },
  },
  {
    label: "Label every merged pull request by area and risk",
    slot: "areas",
    run: async (base, query) => {
      const mix = await fetchReady<WorkMixResponse>(`${base}/work-mix?${query}`, setStepNote);
      renderAreas(mix);
      return `labelled ${mix.work_mix.labelled} merges`;
    },
  },
  {
    label: "Explain what changed and why",
    slot: "answer",
    run: async (base, query) => {
      const insight = await fetchReady<InsightResponse>(`${base}/insights?${query}`, setStepNote);
      renderAnswer(insight);
      return `explained with ${insight.generated_by.model}`;
    },
  },
  {
    label: "Write a team update",
    slot: "digest",
    run: async (base, query) => {
      renderDigest(await fetchReady<DigestResponse>(`${base}/digest?${query}`, setStepNote));
      return "wrote the team update";
    },
  },
];

async function investigate(): Promise<void> {
  if (!current) return;
  const button = byId("explain") as HTMLButtonElement;
  const base = `/v1/repos/${current.repository}`;
  const query = current.query;
  button.disabled = true;
  for (const id of AI_SLOTS) byId(id).replaceChildren();
  const list = byId("steps");
  const items = STEPS.map((step) => el("li", { class: "pending" }, step.label));
  list.replaceChildren(...items);
  list.hidden = false;
  const started = performance.now();
  const done: string[] = [];
  for (const [i, step] of STEPS.entries()) {
    const item = items[i];
    if (!item) continue;
    item.className = "running";
    try {
      done.push(await step.run(base, query));
      item.className = "done";
    } catch (error) {
      // One step failing (quota, GitHub) leaves the others standing.
      item.className = "failed";
      byId(step.slot).replaceChildren(el("p", { class: "status error" }, message(error)));
    }
  }
  const seconds = Math.round((performance.now() - started) / 1000);
  byId("ai-meta").textContent = `The AI ${done.join(", ")} in ${seconds} s.`;
  list.hidden = true;
  button.disabled = false;
}

function setStepNote(note: string): void {
  const running = byId("steps").querySelector("li.running");
  if (running) running.setAttribute("data-note", note);
}

// ---- rendering: the answer -------------------------------------------------------------------

/** A numbered AI section: what it shows, and what the AI did to get there. */
function sectionHead(n: number, title: string, aiDid: string, extra?: HTMLElement): HTMLElement {
  return el(
    "div",
    { class: "section-head" },
    el("span", { class: "step-number" }, String(n)),
    el("div", {}, el("h3", {}, title), el("p", { class: "muted small" }, aiDid)),
    extra ?? null,
  );
}

/** Evidence IDs written into prose, such as "(S2)" or "(C5, T1)": the chips already show them. */
function prose(text: string): string {
  return text.replace(/\s*\((?:[SCUT]\d+(?:,\s*|\s+and\s+)?)+\)/g, "");
}

/** The same names the tiles use, so a change reads the same in both places. */
const METRICS: Record<string, string> = {
  merged_pull_requests: "Merged pull requests",
  wait_for_first_review_hours: "Wait for a first review",
  review_to_approval_hours: "Review to approval",
  approval_to_merge_hours: "Approval to merge",
  external_wait_for_first_review_hours: "Outside contributors' wait",
  maintainer_wait_for_first_review_hours: "Maintainers' wait",
  merged_without_review_rate: "Merged without review",
  top_reviewer_share: "Busiest reviewer's share",
};

function changeCards(insight: InsightResponse, chips: (ids: string[]) => HTMLElement): HTMLElement {
  const real = (lastFlow?.signals ?? []).filter((s) => s.status === "detected");
  if (real.length === 0) {
    return el(
      "p",
      { class: "change none" },
      "No number changed in a statistically real way this period; the AI's reading is below.",
    );
  }
  const explained = new Map(insight.changes.map((c) => [c.signal, c]));
  // Show the numbers the AI was given, so a card always agrees with its explanation.
  const given = new Map(insight.evidence.map((item) => [item.id, item]));
  return el(
    "div",
    { class: "changes" },
    ...real.map((signal) => {
      const why = explained.get(signal.id);
      const fact = given.get(signal.id) ?? signal;
      return el(
        "div",
        { class: "change" },
        el(
          "div",
          { class: "change-head" },
          el("strong", {}, METRICS[signal.metric] ?? signal.description),
          el(
            "span",
            { class: "muted" },
            `${formatValue(fact.baseline, fact.unit)} → ${formatValue(fact.current, fact.unit)}`,
          ),
        ),
        why
          ? el("p", {}, prose(why.explanation), " ", chips(why.evidence))
          : el("p", { class: "muted" }, "The AI gave no specific explanation for this change."),
      );
    }),
  );
}

const CAUSES: Record<string, string> = {
  reviewer_capacity: "Not enough reviewer time",
  review_concentration: "Reviews rest on too few people",
  demand_spike: "More pull requests than reviewers can absorb",
  larger_changes: "Pull requests got bigger",
  merge_process: "Approved work waits to be merged",
  review_coverage_drop: "More merges skip review",
  contributor_mix: "A shift in who contributes",
  waiting_on_authors: "Authors are slow to follow up",
  unresolved_decisions: "Open decisions block work",
  no_significant_change: "Nothing changed significantly",
  other: "Something else",
};

function renderAnswer(insight: InsightResponse): void {
  const h = insight.hypothesis;
  const chips = chipMaker(insight);
  if (insight.generated_by.kind === "fallback") {
    byId("answer").replaceChildren(
      el(
        "div",
        { class: "answer" },
        el(
          "p",
          { class: "status" },
          insight.rejected_because.some((reason) => reason.includes("unavailable"))
            ? "The AI explanation is unavailable right now (the model is busy or out of quota). Try again in a minute."
            : "The AI's explanation did not pass the evidence checks, so it is not shown.",
        ),
        el("ul", {}, ...insight.findings.map((f) => el("li", {}, f.claim))),
      ),
    );
    return;
  }
  const c = h.confidence;
  byId("answer").replaceChildren(
    el(
      "section",
      { class: "answer" },
      sectionHead(
        1,
        "Why the numbers changed",
        "The AI connects each real change above with what it read in the conversations and the labelled merges.",
      ),
      el("p", { class: "headline" }, prose(insight.headline)),
      changeCards(insight, chips),
      el("p", { class: "lead" }, prose(insight.summary)),
      el(
        "div",
        { class: "verdict" },
        el(
          "div",
          {},
          el("div", { class: "label" }, "Likely cause overall"),
          el("strong", {}, CAUSES[h.cause] ?? h.cause),
          el("p", {}, h.statement),
        ),
        el(
          "div",
          {},
          el("div", { class: "label" }, "Confidence"),
          el("strong", { class: `level ${c.level}` }, c.level[0]?.toUpperCase() + c.level.slice(1)),
          el("p", {}, `Because ${c.basis}.`),
        ),
      ),
      el(
        "details",
        { class: "more" },
        el("summary", {}, "How the AI got here"),
        el("h4", {}, "What it found"),
        el(
          "ul",
          {},
          ...insight.findings.map((f) => el("li", {}, prose(f.claim), " ", chips(f.evidence))),
        ),
        h.reasoning && el("p", {}, el("strong", {}, "Reasoning: "), h.reasoning),
        h.alternatives_considered.length > 0 && el("h4", {}, "Ruled out"),
        el("ul", {}, ...h.alternatives_considered.map((a) => el("li", {}, a))),
        h.how_to_verify && el("p", {}, el("strong", {}, "How to check it: "), h.how_to_verify),
        insight.recommended_actions.length > 0 && el("h4", {}, "Suggested plan"),
        el(
          "ul",
          {},
          ...insight.recommended_actions.map((a) =>
            el("li", {}, owner(a.owner), a.action, " ", chips(a.evidence)),
          ),
        ),
        insight.caveats.length > 0 && el("p", { class: "muted small" }, insight.caveats.join(" ")),
        el(
          "details",
          {},
          el("summary", {}, `All ${insight.evidence.length} facts the AI was given`),
          evidenceTable(insight),
        ),
        el(
          "p",
          { class: "muted small" },
          `Written by ${insight.generated_by.model}. Every claim cites its facts; numbers, people and quotes were checked before it was shown. ` +
            "Confidence is a level, not a percentage: it is capped by how strong the statistics are, and a cause read from correlations is never certain.",
        ),
      ),
    ),
  );
}

/** Evidence chips: a T-item jumps to its pull request in the list, anything else to its row. */
function chipMaker(insight: InsightResponse): (ids: string[]) => HTMLElement {
  const items = new Map(insight.evidence.map((item) => [item.id, item]));
  return (ids) => el("span", { class: "chips" }, ...ids.map((id) => chip(id, items.get(id))));
}

function chip(id: string, item: EvidenceItem | undefined): HTMLElement {
  const button = el("button", { type: "button", class: "chip", title: "Show this fact" }, id);
  button.addEventListener("click", () => {
    const row = item?.pull_request ? document.getElementById(`pr-${item.pull_request}`) : null;
    const target = row ?? document.getElementById(`evidence-${id}`);
    if (!target) return;
    for (let node: HTMLElement | null = target; node; node = node.parentElement) {
      if (node instanceof HTMLDetailsElement) node.open = true;
    }
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("highlight");
    setTimeout(() => target.classList.remove("highlight"), 1800);
  });
  return button;
}

function owner(login: string | null): HTMLElement | null {
  return login ? el("span", { class: "owner" }, `@${login.replace(/^@/, "")}`) : null;
}

function evidenceTable(insight: InsightResponse): HTMLElement {
  const rows = insight.evidence.map((item) =>
    el(
      "tr",
      { id: `evidence-${item.id}` },
      el("td", {}, el("code", {}, item.id)),
      el(
        "td",
        {},
        item.label,
        item.pull_request !== null &&
          el(
            "span",
            {},
            " ",
            link(
              `https://github.com/${insight.repository}/pull/${item.pull_request}`,
              `#${item.pull_request}`,
            ),
          ),
        item.detail && el("div", { class: "muted small" }, item.detail),
      ),
      el("td", { class: "num" }, formatValue(item.baseline, item.unit)),
      el("td", { class: "num" }, formatValue(item.current, item.unit)),
    ),
  );
  return el(
    "table",
    {},
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", {}, "ID"),
        el("th", {}, "Fact"),
        el("th", { class: "num" }, "Before"),
        el("th", { class: "num" }, "Now"),
      ),
    ),
    el("tbody", {}, ...rows),
  );
}

// ---- rendering: this week's list ---------------------------------------------------------------

const ACTIONS: Record<NextAction, string> = {
  review_now: "Quick review",
  prioritise: "Review",
  decide: "Decide",
  nudge_author: "Ask author",
  close: "Close",
  wait: "Waiting",
  none: "",
};
const ACTION_ORDER: NextAction[] = [
  "review_now",
  "prioritise",
  "decide",
  "nudge_author",
  "close",
  "wait",
];
const VALUE_ORDER = { high: 0, medium: 1, low: 2 };
const SHOWN_AT_FIRST = 6;

function renderTodo(triage: TriageResponse): void {
  const waiting = triage.pull_requests
    .slice()
    .sort(
      (a, b) =>
        Number(a.diagnosis.action === "wait") - Number(b.diagnosis.action === "wait") ||
        VALUE_ORDER[a.diagnosis.value] - VALUE_ORDER[b.diagnosis.value] ||
        ACTION_ORDER.indexOf(a.diagnosis.action) - ACTION_ORDER.indexOf(b.diagnosis.action) ||
        b.waited_days - a.waited_days,
    );
  const quick = waiting.filter((pr) => pr.diagnosis.action === "review_now").length;
  const high = waiting.filter((pr) => pr.diagnosis.value === "high").length;
  const rows = waiting.map(todoRow);
  const rest = rows.slice(SHOWN_AT_FIRST);
  for (const row of rest) row.hidden = true;
  const more =
    rest.length > 0 && el("button", { type: "button", class: "link" }, `Show ${rest.length} more`);
  if (more) {
    more.addEventListener("click", () => {
      for (const row of rest) row.hidden = false;
      more.remove();
    });
  }
  byId("todo").replaceChildren(
    el(
      "section",
      { class: "block" },
      sectionHead(
        2,
        "What to do this week",
        waiting.length === 0
          ? "Nothing has waited long enough this period to need a reading."
          : `The AI read the ${waiting.length} longest-waiting pull requests: ${quick} quick reviews, ${high} of high value. ` +
              "Most valuable first; open a row for details and a reply to paste on GitHub.",
      ),
      el("div", { class: "rows" }, ...rows),
      more,
    ),
  );
}

function todoRow(pr: TriagedPullRequest): HTMLElement {
  const d = pr.diagnosis;
  return el(
    "details",
    { class: "row", id: `pr-${pr.number}` },
    el(
      "summary",
      {},
      el("span", { class: `act ${d.action}` }, ACTIONS[d.action]),
      el(
        "span",
        { class: "row-main" },
        el("span", { class: "row-title" }, `#${pr.number} ${pr.title}`),
        el("span", { class: "row-sub" }, `${d.summary} · waiting ${days(pr.waited_days)}`),
      ),
      el(
        "span",
        { class: "row-side" },
        owner(d.owner),
        d.reply ? copyButton(d.reply, "Copy reply") : null,
      ),
    ),
    el(
      "div",
      { class: "row-body" },
      el(
        "div",
        { class: "facts" },
        d.value === "high" && el("span", { class: "fact strong" }, "High value"),
        !pr.team_responded && el("span", { class: "fact" }, "No maintainer response yet"),
        pr.ci_checks === "FAILURE" && el("span", { class: "fact" }, "CI failing"),
        pr.merge_conflicts && el("span", { class: "fact" }, "Merge conflicts"),
        el("span", { class: "fact" }, `${pr.author ?? "(deleted)"}, ${pr.author_role}`),
        link(pr.url, "Open on GitHub"),
      ),
      el("p", {}, el("strong", {}, "In the way: "), d.explanation),
      ...pr.quotes.map((q) =>
        el(
          "blockquote",
          {},
          `“${q.text}”`,
          el(
            "cite",
            {},
            "— ",
            q.url ? link(q.url, `${q.who ?? "someone"} (${q.role})`) : `${q.who} (${q.role})`,
          ),
        ),
      ),
      d.reply &&
        el("div", { class: "reply" }, el("div", { class: "label" }, "Reply to post"), d.reply),
    ),
  );
}

// ---- rendering: where the work goes ------------------------------------------------------------

const TYPES: Record<string, string> = {
  feature: "features",
  bug_fix: "bug fixes",
  performance: "performance",
  refactor: "refactoring",
  docs: "docs",
  tests_and_ci: "tests and CI",
  dependencies: "dependencies",
  chore: "chores and releases",
};
const AREAS_SHOWN = 6;
const RISKS_SHOWN = 3;
const MIN_AREA_MERGES = 3;

function renderAreas(response: WorkMixResponse): void {
  const mix = response.work_mix;
  const total = mix.by_type.reduce((sum, t) => sum + t.current, 0);
  const mostly = [...mix.by_type]
    .sort((a, b) => b.current - a.current)
    .slice(0, 3)
    .map((t) => `${TYPES[t.type] ?? t.type} ${percent(total ? t.current / total : 0)}`)
    .join(" · ");
  // The biggest slowdowns first, that is where to look; areas with too few merges to say go last.
  const change = (a: (typeof mix.by_area)[number]) =>
    a.current < MIN_AREA_MERGES ||
    a.median_wait_hours === null ||
    a.baseline_median_wait_hours === null
      ? Number.NEGATIVE_INFINITY
      : a.median_wait_hours - a.baseline_median_wait_hours;
  const ranked = [...mix.by_area].sort((a, b) => change(b) - change(a) || b.current - a.current);
  const areas = ranked.slice(0, AREAS_SHOWN).map((a) => {
    const before = a.baseline_median_wait_hours;
    const now = a.median_wait_hours;
    const trend =
      before === null || now === null
        ? ""
        : now - before >= 4 && now > before * 1.5
          ? "worse"
          : before - now >= 4 && now < before / 1.5
            ? "better"
            : "";
    return el(
      "tr",
      {},
      el("td", { title: a.description }, a.area),
      el("td", { class: "num muted" }, String(a.current)),
      el("td", { class: `num ${trend}` }, `${hours(before)} → ${hours(now)}`),
    );
  });
  const risks = mix.second_look.map((item, i) => {
    const row = el(
      "li",
      {},
      link(item.url, `#${item.number}`),
      ` ${item.title}`,
      el("div", { class: "muted small" }, item.reason),
    );
    row.hidden = i >= RISKS_SHOWN;
    return row;
  });
  const hidden = risks.filter((row) => row.hidden);
  const more =
    hidden.length > 0 &&
    el("button", { type: "button", class: "link" }, `Show ${hidden.length} more`);
  if (more) {
    more.addEventListener("click", () => {
      for (const row of hidden) row.hidden = false;
      more.remove();
    });
  }
  byId("areas").replaceChildren(
    el(
      "section",
      { class: "block" },
      sectionHead(
        3,
        "Where the work goes",
        `The AI labelled ${mix.labelled} merges by area and risk (mostly ${mostly}); code did the counting.`,
      ),
      el(
        "div",
        { class: "pair" },
        el(
          "div",
          {},
          el("h4", {}, "Wait for a first review, by area (biggest slowdowns first)"),
          el(
            "table",
            {},
            el(
              "thead",
              {},
              el(
                "tr",
                {},
                el("th", {}, "Area"),
                el("th", { class: "num" }, "Merges"),
                el("th", { class: "num" }, "Before → now"),
              ),
            ),
            el("tbody", {}, ...areas),
          ),
          el("p", { class: "muted small" }, "Areas with few merges are hints, not proof."),
        ),
        el(
          "div",
          {},
          el("h4", {}, `Merged without review, worth a second look (${risks.length})`),
          risks.length === 0
            ? el("p", { class: "muted small" }, "None: every risky or large change had a review.")
            : el("ul", { class: "risks" }, ...risks),
          more,
        ),
      ),
    ),
  );
}

// ---- rendering: the team update ----------------------------------------------------------------

function renderDigest(response: DigestResponse): void {
  const d = response.digest;
  if (!d || !response.markdown) {
    byId("digest").replaceChildren(
      el("p", { class: "muted small" }, "The team update is unavailable right now."),
    );
    return;
  }
  const section = (title: string, points: DigestPoint[]) =>
    points.length > 0 &&
    el("div", {}, el("h4", {}, title), el("ul", {}, ...points.map((p) => el("li", {}, p.text))));
  byId("digest").replaceChildren(
    el(
      "section",
      { class: "block" },
      sectionHead(
        4,
        "Team update",
        "Written by the AI from the checked results above, ready to paste into a chat.",
        copyButton(response.markdown, "Copy as Markdown"),
      ),
      el("p", {}, el("strong", {}, d.title), " ", d.tldr),
      el(
        "details",
        { class: "more" },
        el("summary", {}, "Preview"),
        section("Shipped", d.shipped),
        section("Needs attention", d.needs_attention),
        section("To decide", d.decisions),
      ),
    ),
  );
}

// ---- rendering: the numbers --------------------------------------------------------------------

function renderFlow(flow: FlowResponse): void {
  lastFlow = flow;
  const now = flow.current;
  const before = flow.baseline;
  const real = new Set(flow.signals.filter((s) => s.status === "detected").map((s) => s.metric));
  byId("title").textContent = flow.repository;
  byId("subtitle").textContent =
    `${flow.period.start.slice(0, 10)} → ${flow.period.end.slice(0, 10)}, compared with the period before` +
    ` · data as of ${new Date(flow.data_as_of).toLocaleString()}${flow.refreshing ? " (refreshing)" : ""}`;

  const tiles = [
    tile(
      "Merged pull requests",
      String(now.merged),
      String(before.merged),
      real.has("merged_pull_requests"),
    ),
    tile(
      "Wait for a first review",
      hours(now.stages.wait_for_first_review?.p50),
      hours(before.stages.wait_for_first_review?.p50),
      real.has("wait_for_first_review_hours"),
    ),
    tile(
      "Still waiting for a first review",
      String(now.waiting_for_first_review_at_end),
      String(before.waiting_for_first_review_at_end),
      false,
    ),
  ];
  // Other metrics earn a tile only when they really changed.
  if (real.has("external_wait_for_first_review_hours")) {
    tiles.push(
      tile(
        "Outside contributors' wait",
        hours(now.wait_for_first_review_by_author.external?.p50),
        hours(before.wait_for_first_review_by_author.external?.p50),
        true,
      ),
    );
  }
  if (real.has("merged_without_review_rate")) {
    tiles.push(
      tile(
        "Merged without review",
        percent(now.merged_without_review_rate),
        percent(before.merged_without_review_rate),
        true,
      ),
    );
  }
  if (real.has("top_reviewer_share")) {
    tiles.push(
      tile(
        "Busiest reviewer's share",
        percent(now.reviewers.top_share),
        percent(before.reviewers.top_share),
        true,
      ),
    );
  }
  byId("tiles").replaceChildren(...tiles);
}

function tile(label: string, now: string, before: string, real: boolean): HTMLElement {
  return el(
    "div",
    { class: real ? "tile real" : "tile" },
    el("div", { class: "label" }, label),
    el("div", { class: "value" }, now),
    el(
      "div",
      { class: "before" },
      `was ${before}`,
      real &&
        el(
          "span",
          { class: "badge detected", title: "Large and statistically significant, not noise" },
          "real change",
        ),
    ),
  );
}

// ---- wiring ----------------------------------------------------------------------------------

byId("form").addEventListener("submit", (event) => void analyse(event));
byId("explain").addEventListener("click", () => void investigate());
for (const button of document.querySelectorAll<HTMLButtonElement>("[data-repo]")) {
  button.addEventListener("click", () => {
    (byId("repo") as HTMLInputElement).value = button.dataset.repo ?? "";
    void analyse();
  });
}
