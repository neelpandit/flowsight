"use strict";
// Flowsight UI: one page over the JSON API. No framework, no bundler.
// Decision: plain TypeScript compiled by tsc, no React and no bundler. The page is a handful of
// sections that render once per answer, so a framework would add a build chain and hundreds of
// dependencies for nothing. The compiled app.js is committed, so running it needs no Node.
// Decision: every string from the API is inserted with textContent, never as HTML: pull
// request titles and comments are written by strangers, and this rules out script injection
// (the strict Content-Security-Policy set by the server is the second layer).
class ApiError extends Error {
}
/** Creates an element. Strings become text nodes, so API data can never inject markup. */
function el(tag, attributes = {}, ...children) {
    const element = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes))
        element.setAttribute(name, value);
    for (const child of children) {
        if (child === null || child === undefined || child === false)
            continue;
        element.append(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return element;
}
function byId(id) {
    const element = document.getElementById(id);
    if (!element)
        throw new Error(`missing #${id}`);
    return element;
}
const hours = (value) => value === null || value === undefined ? "–" : `${value.toFixed(1)} h`;
const percent = (value) => value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
const days = (value) => `${Math.round(value)} days`;
function formatValue(value, unit) {
    if (value === null)
        return "–";
    if (unit === "hours")
        return hours(value);
    if (unit === "ratio")
        return percent(value);
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
function link(href, text) {
    return el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
}
function message(error) {
    return error instanceof Error ? error.message : String(error);
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/** Copies text to the clipboard. The page never posts anything itself. */
function copyButton(text, label) {
    const button = el("button", { type: "button", class: "copy" }, label);
    button.addEventListener("click", (event) => {
        event.preventDefault(); // inside a <summary>, a click would also open or close the row
        event.stopPropagation();
        void navigator.clipboard.writeText(text).then(() => {
            button.textContent = "Copied";
            setTimeout(() => (button.textContent = label), 1500);
        }, () => (button.textContent = "Copy failed"));
    });
    return button;
}
// ---- API calls -------------------------------------------------------------------------------
async function getJson(path) {
    const response = await fetch(path, { headers: { accept: "application/json" } });
    const body = await response.json();
    if (!response.ok && response.status !== 202) {
        const problem = body;
        throw new ApiError(problem.detail ?? problem.title ?? `Request failed (${response.status})`);
    }
    return { status: response.status, body: body };
}
/** Fetches a repository resource, waiting out a first sync (202 Accepted) if one starts. */
async function fetchReady(path, onProgress) {
    for (let attempt = 0; attempt < 60; attempt++) {
        const { status, body } = await getJson(path);
        if (status !== 202)
            return body;
        onProgress("Fetching pull requests from GitHub for the first time… this can take a minute.");
        await waitForSync(body);
    }
    throw new ApiError("The sync is taking unusually long; try again shortly.");
}
async function waitForSync(accepted) {
    for (let attempt = 0; attempt < 60; attempt++) {
        await sleep(Math.min(accepted.retry_after_seconds, 3) * 1000);
        const { body } = await getJson(accepted.status_url);
        if (body.job?.status !== "running")
            return;
    }
}
// ---- state and the main flow -----------------------------------------------------------------
let current = null;
let lastFlow = null;
function periodQuery(numberOfDays) {
    const end = new Date();
    end.setUTCMinutes(0, 0, 0);
    const start = new Date(end.getTime() - numberOfDays * 86_400_000);
    return `from=${encodeURIComponent(start.toISOString())}&to=${encodeURIComponent(end.toISOString())}`;
}
function setStatus(text, kind = "info") {
    const status = byId("status");
    status.textContent = text;
    status.className = `status ${kind}`;
    status.hidden = text === "";
}
const AI_SLOTS = ["answer", "todo"];
const INTRO = byId("ai-meta").textContent ?? "";
async function analyse(event) {
    event?.preventDefault();
    const slug = byId("repo").value.trim();
    const numberOfDays = Number(byId("days").value);
    if (!/^[\w.-]+\/[\w.-]+$/.test(slug)) {
        setStatus("Enter a repository as owner/name, e.g. withastro/astro.", "error");
        return;
    }
    const base = `/v1/repos/${slug}`;
    current = { repository: slug, query: periodQuery(numberOfDays) };
    byId("results").hidden = true;
    for (const id of AI_SLOTS)
        byId(id).replaceChildren();
    byId("ai-meta").textContent = INTRO;
    setStatus("Loading…");
    try {
        const flow = await fetchReady(`${base}/flow?${current.query}`, setStatus);
        current.repository = flow.repository;
        renderFlow(flow);
        setStatus("");
        byId("results").hidden = false;
    }
    catch (error) {
        setStatus(message(error), "error");
    }
}
const STEPS = [
    {
        label: "Read the conversations of the longest-waiting pull requests",
        slot: "todo",
        run: async (base, query) => {
            const triage = await fetchReady(`${base}/triage?${query}`, setStepNote);
            renderTodo(triage);
            return `read ${triage.pull_requests.length} conversations`;
        },
    },
    {
        label: "Explain what changed and why",
        slot: "answer",
        run: async (base, query) => {
            const insight = await fetchReady(`${base}/insights?${query}`, setStepNote);
            renderAnswer(insight);
            return `explained with ${insight.generated_by.model}`;
        },
    },
];
async function investigate() {
    if (!current)
        return;
    const button = byId("explain");
    const base = `/v1/repos/${current.repository}`;
    const query = current.query;
    button.disabled = true;
    for (const id of AI_SLOTS)
        byId(id).replaceChildren();
    const list = byId("steps");
    const items = STEPS.map((step) => el("li", { class: "pending" }, step.label));
    list.replaceChildren(...items);
    list.hidden = false;
    const started = performance.now();
    const done = [];
    for (const [i, step] of STEPS.entries()) {
        const item = items[i];
        if (!item)
            continue;
        item.className = "running";
        try {
            done.push(await step.run(base, query));
            item.className = "done";
        }
        catch (error) {
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
function setStepNote(note) {
    const running = byId("steps").querySelector("li.running");
    if (running)
        running.setAttribute("data-note", note);
}
// ---- rendering: the answer -------------------------------------------------------------------
/** A numbered AI section: what it shows, and what the AI did to get there. */
function sectionHead(n, title, aiDid, extra) {
    return el("div", { class: "section-head" }, el("span", { class: "step-number" }, String(n)), el("div", {}, el("h3", {}, title), el("p", { class: "muted small" }, aiDid)), extra ?? null);
}
/** Evidence IDs written into prose, such as "(S2)" or "(C5, C9)": the chips already show them. */
function prose(text) {
    return text.replace(/\s*\((?:[SC]\d+(?:,\s*|\s+and\s+)?)+\)/g, "");
}
/** The same names the tiles use, so a change reads the same in both places. */
const METRICS = {
    wait_for_first_review_hours: "Wait for a first review",
    review_to_approval_hours: "First review to approval",
    approval_to_merge_hours: "Approval to merge",
};
function changeCards(insight, chips) {
    const real = (lastFlow?.signals ?? []).filter((s) => s.status === "detected");
    if (real.length === 0) {
        return el("p", { class: "change none" }, "No number changed in a statistically real way this period; the AI's reading is below.");
    }
    const explained = new Map(insight.changes.map((c) => [c.signal, c]));
    // Show the numbers the AI was given, so a card always agrees with its explanation.
    const given = new Map(insight.evidence.map((item) => [item.id, item]));
    return el("div", { class: "changes" }, ...real.map((signal) => {
        const why = explained.get(signal.id);
        const fact = given.get(signal.id) ?? signal;
        return el("div", { class: "change" }, el("div", { class: "change-head" }, el("strong", {}, METRICS[signal.metric] ?? signal.description), el("span", { class: "muted" }, `${formatValue(fact.baseline, fact.unit)} → ${formatValue(fact.current, fact.unit)}`)), why
            ? el("p", {}, prose(why.explanation), " ", chips(why.evidence))
            : el("p", { class: "muted" }, "The AI gave no specific explanation for this change."));
    }));
}
const CAUSES = {
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
function renderAnswer(insight) {
    const h = insight.hypothesis;
    const chips = chipMaker();
    if (insight.generated_by.kind === "fallback") {
        byId("answer").replaceChildren(el("div", { class: "answer" }, el("p", { class: "status" }, insight.rejected_because.some((reason) => reason.includes("unavailable"))
            ? "The AI explanation is unavailable right now (the model is busy or out of quota). Try again in a minute."
            : "The AI's explanation did not pass the evidence checks, so it is not shown."), el("ul", {}, ...insight.findings.map((f) => el("li", {}, f.claim)))));
        return;
    }
    const c = h.confidence;
    byId("answer").replaceChildren(el("section", { class: "answer" }, sectionHead(1, "Why the numbers changed", "The AI connects each real change above with the context numbers: volume, reviewers, the queue."), el("p", { class: "headline" }, prose(insight.headline)), changeCards(insight, chips), el("p", { class: "lead" }, prose(insight.summary)), el("div", { class: "verdict" }, el("div", {}, el("div", { class: "label" }, "Likely cause overall"), el("strong", {}, CAUSES[h.cause] ?? h.cause), el("p", {}, h.statement)), el("div", {}, el("div", { class: "label" }, "Confidence"), el("strong", { class: `level ${c.level}` }, c.level[0]?.toUpperCase() + c.level.slice(1)), el("p", {}, `Because ${c.basis}.`))), el("details", { class: "more" }, el("summary", {}, "How the AI got here"), el("h4", {}, "What it found"), el("ul", {}, ...insight.findings.map((f) => el("li", {}, prose(f.claim), " ", chips(f.evidence)))), h.reasoning && el("p", {}, el("strong", {}, "Reasoning: "), h.reasoning), insight.caveats.length > 0 && el("p", { class: "muted small" }, insight.caveats.join(" ")), el("details", {}, el("summary", {}, `All ${insight.evidence.length} facts the AI was given`), evidenceTable(insight)), el("p", { class: "muted small" }, `Written by ${insight.generated_by.model}. Every claim cites its facts; numbers, people and quotes were checked before it was shown. ` +
        "Confidence is a level, not a percentage: it is capped by how strong the statistics are, and a cause read from correlations is never certain."))));
}
/** Evidence chips: each jumps to its row in the facts table. */
function chipMaker() {
    return (ids) => el("span", { class: "chips" }, ...ids.map((id) => chip(id)));
}
function chip(id) {
    const button = el("button", { type: "button", class: "chip", title: "Show this fact" }, id);
    button.addEventListener("click", () => {
        const target = document.getElementById(`evidence-${id}`);
        if (!target)
            return;
        for (let node = target; node; node = node.parentElement) {
            if (node instanceof HTMLDetailsElement)
                node.open = true;
        }
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.add("highlight");
        setTimeout(() => target.classList.remove("highlight"), 1800);
    });
    return button;
}
function owner(login) {
    return login ? el("span", { class: "owner" }, `@${login.replace(/^@/, "")}`) : null;
}
function evidenceTable(insight) {
    const rows = insight.evidence.map((item) => el("tr", { id: `evidence-${item.id}` }, el("td", {}, el("code", {}, item.id)), el("td", {}, item.label), el("td", { class: "num" }, formatValue(item.baseline, item.unit)), el("td", { class: "num" }, formatValue(item.current, item.unit))));
    return el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "ID"), el("th", {}, "Fact"), el("th", { class: "num" }, "Before"), el("th", { class: "num" }, "Now"))), el("tbody", {}, ...rows));
}
// ---- rendering: this week's list ---------------------------------------------------------------
const ACTIONS = {
    review_now: "Quick review",
    prioritise: "Review",
    decide: "Decide",
    nudge_author: "Ask author",
    close: "Close",
    wait: "Waiting",
    none: "",
};
const ACTION_ORDER = [
    "review_now",
    "prioritise",
    "decide",
    "nudge_author",
    "close",
    "wait",
];
// Decision: the list is ordered by what can be finished soonest (a quick review before a
// decision), then by wait, with "waiting on something else" last. Only six are shown at first
// so the section stays scannable.
const SHOWN_AT_FIRST = 6;
function renderTodo(triage) {
    const waiting = triage.pull_requests
        .slice()
        .sort((a, b) => Number(a.diagnosis.action === "wait") - Number(b.diagnosis.action === "wait") ||
        ACTION_ORDER.indexOf(a.diagnosis.action) - ACTION_ORDER.indexOf(b.diagnosis.action) ||
        b.waited_days - a.waited_days);
    const quick = waiting.filter((pr) => pr.diagnosis.action === "review_now").length;
    const rows = waiting.map(todoRow);
    const rest = rows.slice(SHOWN_AT_FIRST);
    for (const row of rest)
        row.hidden = true;
    const more = rest.length > 0 && el("button", { type: "button", class: "link" }, `Show ${rest.length} more`);
    if (more) {
        more.addEventListener("click", () => {
            for (const row of rest)
                row.hidden = false;
            more.remove();
        });
    }
    byId("todo").replaceChildren(el("section", { class: "block" }, sectionHead(2, "What to do this week", waiting.length === 0
        ? "Nothing has waited long enough this period to need a reading."
        : `The AI read the ${waiting.length} longest-waiting pull requests; ${quick} can be finished with a quick review. ` +
            "Open a row for details and a reply to paste on GitHub."), el("div", { class: "rows" }, ...rows), more));
}
function todoRow(pr) {
    const d = pr.diagnosis;
    return el("details", { class: "row", id: `pr-${pr.number}` }, el("summary", {}, el("span", { class: `act ${d.action}` }, ACTIONS[d.action]), el("span", { class: "row-main" }, el("span", { class: "row-title" }, `#${pr.number} ${pr.title}`), el("span", { class: "row-sub" }, `${d.summary} · waiting ${days(pr.waited_days)}`)), el("span", { class: "row-side" }, owner(d.owner), d.reply ? copyButton(d.reply, "Copy reply") : null)), el("div", { class: "row-body" }, el("div", { class: "facts" }, !pr.team_responded && el("span", { class: "fact" }, "No maintainer response yet"), pr.ci_checks === "FAILURE" && el("span", { class: "fact" }, "CI failing"), pr.merge_conflicts && el("span", { class: "fact" }, "Merge conflicts"), el("span", { class: "fact" }, `${pr.author ?? "(deleted)"}, ${pr.author_role}`), link(pr.url, "Open on GitHub")), el("p", {}, el("strong", {}, "In the way: "), d.explanation), ...pr.quotes.map((q) => el("blockquote", {}, `“${q.text}”`, el("cite", {}, "— ", q.url ? link(q.url, `${q.who ?? "someone"} (${q.role})`) : `${q.who} (${q.role})`))), d.reply &&
        el("div", { class: "reply" }, el("div", { class: "label" }, "Reply to post"), d.reply)));
}
// ---- rendering: the numbers --------------------------------------------------------------------
function renderFlow(flow) {
    lastFlow = flow;
    const now = flow.current;
    const before = flow.baseline;
    const real = new Set(flow.signals.filter((s) => s.status === "detected").map((s) => s.metric));
    byId("title").textContent = flow.repository;
    byId("subtitle").textContent =
        `${flow.period.start.slice(0, 10)} → ${flow.period.end.slice(0, 10)}, compared with the period before` +
            ` · data as of ${new Date(flow.data_as_of).toLocaleString()}${flow.refreshing ? " (refreshing)" : ""}`;
    // Decision: the tiles are the three stages where a merged pull request waits, in order, so
    // the row reads as the journey: first review, then approval, then merge. Merges and the open
    // queue are not wait times, so they sit in a context line below rather than among the tiles.
    const stages = [
        [
            "wait_for_first_review_hours",
            "Wait for a first review",
            now.stages.wait_for_first_review,
            before.stages.wait_for_first_review,
        ],
        [
            "review_to_approval_hours",
            "First review to approval",
            now.stages.review_to_approval,
            before.stages.review_to_approval,
        ],
        [
            "approval_to_merge_hours",
            "Approval to merge",
            now.stages.approval_to_merge,
            before.stages.approval_to_merge,
        ],
    ];
    byId("tiles").replaceChildren(...stages.map(([metric, label, after, prior]) => tile(label, hours(after?.p50), hours(prior?.p50), real.has(metric))));
    // Decision: "still waiting" is a snapshot on the period's last day, not a median over the
    // period, so it is never badged as a real change (see analytics/signals.py).
    byId("context").textContent =
        `${now.merged} pull requests merged (was ${before.merged}) · ` +
            `${now.waiting_for_first_review_at_end} still waiting for a first review at the end of the period (was ${before.waiting_for_first_review_at_end})`;
}
function tile(label, now, before, real) {
    return el("div", { class: real ? "tile real" : "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, now), el("div", { class: "before" }, `was ${before}`, real &&
        el("span", { class: "badge detected", title: "Large and statistically significant, not noise" }, "real change")));
}
// ---- wiring ----------------------------------------------------------------------------------
byId("form").addEventListener("submit", (event) => void analyse(event));
byId("explain").addEventListener("click", () => void investigate());
for (const button of document.querySelectorAll("[data-repo]")) {
    button.addEventListener("click", () => {
        byId("repo").value = button.dataset.repo ?? "";
        if (button.dataset.days)
            byId("days").value = button.dataset.days;
        void analyse();
    });
}
