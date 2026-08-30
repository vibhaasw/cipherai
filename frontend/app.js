"use strict";

const API_BASE = ""; // same origin — served by the FastAPI app itself

const chatHistoryEl = document.getElementById("chat-history");
const promptInput = document.getElementById("prompt-input");
const sendBtn = document.getElementById("send-btn");
const charCountEl = document.getElementById("char-count");
const statusContainer = document.getElementById("status-container");
const continuationsContainer = document.getElementById("continuations-container");
const summaryMetricsEl = document.getElementById("summary-metrics");
const lastUpdatedEl = document.getElementById("last-updated");
const backendIndicator = document.getElementById("backend-indicator");
const decisionLogEl = document.getElementById("decision-log");

const exchanges = []; // session-only history
const decisionLog = []; // session-only decision log (newest first)
let lastProvider = null; // provider used by the previous request
let lastDomain = null; // domain classified for the previous request
let exchangeSeq = 0;

function esc(value) {
  const div = document.createElement("div");
  div.textContent = String(value ?? "");
  return div.innerHTML;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/* ---------------- Chat / Console ---------------- */

function attemptReason(a) {
  return a.reason ?? a.decision_reason ?? a.error ?? "-";
}

function attemptLineHtml(a, extraClass) {
  const status = String(a.status ?? "-");
  const ok = status === "success";
  return `<div class="trace-line ${ok ? "ok" : "fail"} ${extraClass || ""}">` +
    `<div class="dot"></div>` +
    `<span class="label">#${esc(a.rank ?? "-")} ${esc(a.provider ?? "-")} / ${esc(a.model ?? "-")}</span>` +
    `<span class="st">${esc(status)}</span>` +
    `<span class="reason">${esc(attemptReason(a))}</span>` +
    `</div>`;
}

function renderAttempts(attempts) {
  if (!Array.isArray(attempts) || attempts.length === 0) {
    return '<div class="trace"><div class="trace-title">Routing Trail</div><div class="trace-line"><div class="dot"></div><span class="reason">no attempts recorded</span></div></div>';
  }
  const lines = attempts.map((a) => attemptLineHtml(a)).join("");
  return `<div class="trace"><div class="trace-title">Routing Trail (${attempts.length})</div>${lines}</div>`;
}

function changeCalloutHtml(exchange) {
  const c = exchange.changes;
  if (!c || (!c.provider && !c.domain)) return "";
  const parts = [];
  if (c.domain) parts.push(`DOMAIN CHANGED (${esc(c.domain.from)} \u2192 ${esc(c.domain.to)})`);
  if (c.provider) parts.push(`\u26a1 PROVIDER CHANGED: ${esc(c.provider.from)} \u2192 ${esc(c.provider.to)}`);
  const why = c.reason ? `<span class="why">${esc(c.reason)}</span>` : "";
  return `<div class="change-callout">${parts.join(" \u2192 ")}${why}</div>`;
}

function renderExchange(exchange) {
  const wrap = document.createElement("div");
  wrap.className = "panel exchange";

  let body;
  if (exchange.state === "loading") {
    body = '<div class="loading">ROUTING PROMPT THROUGH PIPELINE...</div>';
  } else if (exchange.state === "animating") {
    // Live decision trace fills in sequentially; completion revealed after.
    body = `<div class="trace"><div class="trace-title">Decision Trace</div><div id="anim-${exchange.id}"></div></div>`;
  } else if (exchange.state === "network_error") {
    body = `<div class="error-msg">BACKEND UNREACHABLE: ${esc(exchange.message)}</div>`;
  } else if (exchange.response && exchange.response.error) {
    const r = exchange.response;
    body =
      changeCalloutHtml(exchange) +
      `<div class="error-msg">${esc(r.error)}: ${esc(r.message ?? "")}</div>` +
      renderAttempts(r.attempts);
  } else {
    const r = exchange.response;
    body =
      changeCalloutHtml(exchange) +
      `<pre class="completion">${esc(r.completion)}</pre>` +
      `<div class="result-row"><div class="k">Domain</div><div class="v accent">${esc(r.domain)}</div></div>` +
      `<div class="result-row"><div class="k">Complexity</div><div class="v">${esc(r.complexity)}</div></div>` +
      `<div class="result-row"><div class="k">Provider</div><div class="v">${esc(r.provider)}</div></div>` +
      `<div class="result-row"><div class="k">Model</div><div class="v">${esc(r.model)}</div></div>` +
      renderAttempts(r.attempts);
  }

  wrap.innerHTML = `<div class="user-prompt">${esc(exchange.prompt)}</div>` + body;
  return wrap;
}

function renderChatHistory() {
  chatHistoryEl.innerHTML = "";
  if (exchanges.length === 0) {
    chatHistoryEl.innerHTML =
      '<div class="panel"><div class="result-empty">&mdash; NO REQUEST ROUTED YET &mdash;</div></div>';
    return;
  }
  // newest first so the latest exchange sits directly under the console
  for (let i = exchanges.length - 1; i >= 0; i--) {
    chatHistoryEl.appendChild(renderExchange(exchanges[i]));
  }
}

/* ----- live decision trace animation (cosmetic pacing over real data) ----- */

function buildTraceLines(r) {
  const lines = [];
  lines.push(
    `<div class="trace-line step"><div class="dot"></div><span class="label">CLASSIFYING PROMPT...</span></div>`
  );
  if (r.domain) {
    lines.push(
      `<div class="trace-line ok"><div class="dot"></div>` +
      `<span class="label">CLASSIFIED</span>` +
      `<span class="reason">domain=${esc(r.domain)} \u00b7 complexity=${esc(r.complexity)}</span></div>`
    );
  }
  for (const a of r.attempts ?? []) {
    lines.push(attemptLineHtml(a));
  }
  if (r.error) {
    lines.push(
      `<div class="trace-line fail"><div class="dot"></div>` +
      `<span class="label">${esc(r.error)}</span><span class="reason">${esc(r.message ?? "")}</span></div>`
    );
  } else if (r.provider) {
    lines.push(
      `<div class="trace-line ok"><div class="dot"></div>` +
      `<span class="label">DISPATCHED TO ${esc(r.provider)} / ${esc(r.model)}</span></div>`
    );
  }
  return lines;
}

async function animateDecisionTrace(exchange) {
  exchange.state = "animating";
  renderChatHistory();
  const container = document.getElementById(`anim-${exchange.id}`);
  if (container) {
    const lines = buildTraceLines(exchange.response);
    for (const html of lines) {
      container.insertAdjacentHTML("beforeend", html);
      container.lastElementChild.classList.add("reveal-in");
      await sleep(220);
    }
    await sleep(300);
  }
  exchange.state = "done";
  renderChatHistory();
}

/* ----- provider/domain change detection (client-side state only) ----- */

function detectChanges(r) {
  const changes = { provider: null, domain: null, reason: null };
  if (r.provider && lastProvider && r.provider !== lastProvider) {
    changes.provider = { from: lastProvider, to: r.provider };
    // Actual reason from this request's attempts: the selected attempt's
    // reason explains why this provider won; prior failed attempts explain
    // any failover. Prefer the failure reason when one exists.
    const attempts = r.attempts ?? [];
    const failed = attempts.filter((a) => String(a.status ?? "") !== "success");
    const selected = attempts.find((a) => String(a.status ?? "") === "success");
    if (failed.length > 0) {
      const f = failed[0];
      changes.reason = `#${f.rank ?? "-"} ${f.provider ?? "-"}: ${attemptReason(f)}`;
    } else if (selected) {
      changes.reason = attemptReason(selected);
    }
  }
  if (r.domain && lastDomain && r.domain !== lastDomain) {
    changes.domain = { from: lastDomain, to: r.domain };
  }
  if (r.provider) lastProvider = r.provider;
  if (r.domain) lastDomain = r.domain;
  return changes;
}

/* ----- session decision log ----- */

function summarizeDecision(r) {
  if (r.error) {
    const n = (r.attempts ?? []).length;
    return n > 0
      ? `${r.error} — all ${n} evaluated candidates unavailable`
      : `${r.error}`;
  }
  const attempts = r.attempts ?? [];
  const selected = attempts.find((a) => String(a.status ?? "") === "success");
  const failed = attempts.filter((a) => String(a.status ?? "") !== "success");
  if (selected && failed.length === 0) {
    return `Rank #${selected.rank ?? "-"} ${selected.provider ?? "-"} healthy, selected directly`;
  }
  if (selected) {
    const first = failed[0];
    return `Rank #${first.rank ?? "-"} ${first.provider ?? "-"} ${String(first.status ?? "failed")}, ` +
      `failed over to Rank #${selected.rank ?? "-"} ${selected.provider ?? "-"} ` +
      `(${failed.length} candidate${failed.length > 1 ? "s" : ""} skipped/failed)`;
  }
  return "no successful attempt recorded";
}

function addLogEntry(exchange) {
  const r = exchange.response;
  decisionLog.unshift({
    time: new Date().toLocaleTimeString(),
    domain: r.domain ?? "-",
    provider: r.provider ?? null,
    model: r.model ?? null,
    error: r.error ?? null,
    summary: summarizeDecision(r),
    attempts: r.attempts ?? [],
    open: false,
  });
  renderDecisionLog();
}

function renderDecisionLog() {
  if (decisionLog.length === 0) {
    decisionLogEl.innerHTML =
      '<div class="result-empty">&mdash; NO DECISIONS LOGGED YET &mdash;</div>';
    return;
  }
  decisionLogEl.innerHTML = "";
  decisionLog.forEach((entry, idx) => {
    const item = document.createElement("div");
    item.className = "log-item" + (entry.open ? " open" : "");
    const modelHtml = entry.error
      ? `<span class="log-model log-error">${esc(entry.error)}</span>`
      : `<span class="log-model">${esc(entry.provider)} / ${esc(entry.model)}</span>`;
    item.innerHTML =
      `<div class="log-row">` +
      `<span class="log-time">${esc(entry.time)}</span>` +
      `<span class="log-domain">${esc(entry.domain)}</span>` +
      modelHtml +
      `<span class="log-caret">\u25b6</span>` +
      `</div>` +
      `<div class="log-summary">${esc(entry.summary)}</div>` +
      `<div class="log-detail">${renderAttempts(entry.attempts)}</div>`;
    item.addEventListener("click", () => {
      entry.open = !entry.open;
      renderDecisionLog();
    });
    decisionLogEl.appendChild(item);
  });
}

/* ----- submit ----- */

async function submitPrompt() {
  const prompt = promptInput.value.trim();
  if (!prompt) return;

  promptInput.value = "";
  updateCharCount();
  sendBtn.disabled = true;

  const exchange = { id: ++exchangeSeq, prompt, state: "loading", response: null, changes: null };
  exchanges.push(exchange);
  renderChatHistory();

  try {
    const res = await fetch(`${API_BASE}/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    exchange.response = await res.json();
    exchange.changes = detectChanges(exchange.response);
    await animateDecisionTrace(exchange);
    addLogEntry(exchange);
  } catch (err) {
    exchange.state = "network_error";
    exchange.message = err.message || "fetch failed";
    renderChatHistory();
  } finally {
    sendBtn.disabled = false;
    promptInput.focus();
  }
}

sendBtn.addEventListener("click", submitPrompt);

promptInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submitPrompt();
  }
});

function updateCharCount() {
  charCountEl.textContent = `${promptInput.value.length} CHARS \u00b7 ENTER TO ROUTE`;
}
promptInput.addEventListener("input", updateCharCount);

/* ---------------- Live System Dashboard ---------------- */

const GAUGE_CIRC = 2 * Math.PI * 20;

function gaugeSvg(remaining, limit) {
  const rem = parseFloat(remaining);
  const lim = parseFloat(limit);
  let frac = null; // fraction remaining
  if (!Number.isNaN(rem) && !Number.isNaN(lim) && lim > 0 && rem >= 0) {
    frac = Math.min(Math.max(rem / lim, 0), 1);
  }

  let offset;
  let color;
  if (frac === null) {
    offset = GAUGE_CIRC; // empty arc when limit is unknown (-1 in Redis)
    color = "var(--faint)";
  } else {
    offset = GAUGE_CIRC - frac * GAUGE_CIRC;
    const used = 1 - frac;
    color = used > 0.85 ? "var(--red)" : used > 0.6 ? "var(--amber)" : "var(--accent)";
  }

  return `<div class="gauge"><svg viewBox="0 0 48 48" width="52" height="52">` +
    `<circle class="bg" cx="24" cy="24" r="20"></circle>` +
    `<circle class="fg" cx="24" cy="24" r="20" stroke="${color}" stroke-dasharray="${GAUGE_CIRC.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"></circle>` +
    `</svg></div>`;
}

function usageText(remaining, limit) {
  const rem = parseFloat(remaining);
  const lim = parseFloat(limit);
  if (Number.isNaN(rem) || rem < 0) return "N/A";
  if (Number.isNaN(lim) || lim <= 0) return `${remaining} LEFT`;
  return `${remaining} / ${limit} LEFT`;
}

function renderStatusCards(rows) {
  if (!Array.isArray(rows) || rows.length === 0) {
    statusContainer.innerHTML =
      '<div class="panel"><div class="result-empty">NO QUOTA DATA YET &mdash; A PROVIDER/KEY APPEARS AFTER ITS FIRST REQUEST</div></div>';
    return;
  }

  const now = Date.now() / 1000;
  const sorted = [...rows].sort((a, b) =>
    `${a.provider ?? ""}${a.credential_ref ?? ""}`.localeCompare(`${b.provider ?? ""}${b.credential_ref ?? ""}`)
  );

  statusContainer.innerHTML = sorted
    .map((row) => {
      const status = String(row.status ?? "unknown");
      const tagClass = ["healthy", "near_cap", "cooling_down"].includes(status) ? status : "unknown";

      let resetHtml = "";
      if (status === "cooling_down") {
        const resetAt = parseFloat(row.reset_requests_at ?? 0);
        if (!Number.isNaN(resetAt) && resetAt > 0) {
          const delta = Math.max(Math.round(resetAt - now), 0);
          resetHtml = `<div class="reset">RESET IN ${delta}s</div>`;
        }
      }

      return `<div class="provider-card">` +
        `<div class="provider-head">` +
        `<div class="name">${esc(row.provider ?? "-")}</div>` +
        `<div class="health-tag ${tagClass}">${esc(status.replace("_", " "))}</div>` +
        `</div>` +
        `<div class="provider-cred">${esc(row.credential_ref ?? "-")}</div>` +
        `<div class="profile-row">` +
        gaugeSvg(row.remaining_requests, row.limit_requests) +
        `<div class="profile-info">` +
        `<div class="pid">Requests</div>` +
        `<div class="usage">${esc(usageText(row.remaining_requests, row.limit_requests))}</div>` +
        resetHtml +
        `</div>` +
        `</div>` +
        `<div class="profile-row">` +
        gaugeSvg(row.remaining_tokens, row.limit_tokens) +
        `<div class="profile-info">` +
        `<div class="pid">Tokens</div>` +
        `<div class="usage">${esc(usageText(row.remaining_tokens, row.limit_tokens))}</div>` +
        `</div>` +
        `</div>` +
        `</div>`;
    })
    .join("");
}

function renderContinuationsTable(events) {
  if (!Array.isArray(events) || events.length === 0) {
    continuationsContainer.innerHTML =
      '<div class="result-empty">NO CONTINUATIONS RECORDED YET</div>';
    return;
  }

  const body = events
    .map((ev) => {
      const ts = parseInt(ev.timestamp ?? 0, 10);
      const when = ts > 0 ? new Date(ts * 1000).toLocaleTimeString() : "-";
      const domain = String(ev.domain ?? "-");
      const fromTo = `${ev.original_provider ?? "-"} \u2192 ${ev.fallback_provider ?? "-"}`;

      const compRaw = String(ev.compression_used ?? "N/A").toLowerCase();
      const compression = compRaw === "yes" ? "\u2713" : compRaw === "no" ? "\u2717" : "N/A";

      let savedHtml = '<span class="dim">N/A</span>';
      if (domain !== "CODE_GEN") {
        const pct = parseFloat(ev.tokens_saved_pct ?? 0);
        if (!Number.isNaN(pct) && pct >= 0) {
          const cls = pct > 30 ? "saved-good" : "saved-low";
          savedHtml = `<span class="${cls}">${pct.toFixed(1)}%</span>`;
        }
      }

      const status = String(ev.final_status ?? "-");
      const statusCls = status === "success" ? "st-success" : "st-failed";

      return `<tr>` +
        `<td>${esc(when)}</td>` +
        `<td>${esc(domain)}</td>` +
        `<td>${esc(fromTo)}</td>` +
        `<td>${esc(compression)}</td>` +
        `<td>${savedHtml}</td>` +
        `<td><span class="${statusCls}">${esc(status)}</span></td>` +
        `</tr>`;
    })
    .join("");

  continuationsContainer.innerHTML =
    `<table><thead><tr>` +
    `<th>Time</th><th>Domain</th><th>From \u2192 To</th>` +
    `<th>Compression</th><th>Tokens Saved %</th><th>Status</th>` +
    `</tr></thead><tbody>${body}</tbody></table>`;
}

function renderSummaryMetrics(summary) {
  const total = summary.total_count ?? 0;
  const avg = Number(summary.avg_tokens_saved_pct ?? 0).toFixed(1);
  const compressed = summary.compressed_count ?? 0;
  const eligible = summary.eligible_count ?? 0;
  const fallback = summary.fallback_count ?? 0;
  summaryMetricsEl.innerHTML =
    `<div class="metric-cell"><div class="val">${esc(total)}</div><div class="lbl">Continuations Total</div></div>` +
    `<div class="metric-cell"><div class="val">${esc(avg)}%</div><div class="lbl">Avg Tokens Saved (Compressed Domains)</div></div>` +
    `<div class="metric-cell"><div class="val">${esc(compressed)}/${esc(eligible)}</div><div class="lbl">Compression Used / Eligible Calls</div></div>` +
    `<div class="metric-cell"><div class="val">${esc(fallback)}</div><div class="lbl">Ollama Fallbacks Triggered</div></div>`;
}

function setBackendUp(up) {
  backendIndicator.className = `health-tag ${up ? "healthy" : "cooling_down"}`;
  backendIndicator.textContent = up ? "BACKEND CONNECTED" : "WAITING FOR BACKEND";
}

async function pollDashboard() {
  try {
    const [statusRes, contRes] = await Promise.all([
      fetch(`${API_BASE}/status`),
      fetch(`${API_BASE}/continuations`),
    ]);
    if (!statusRes.ok || !contRes.ok) throw new Error("bad response");

    const statusData = await statusRes.json();
    const contData = await contRes.json();

    renderStatusCards(statusData);
    renderContinuationsTable(contData.events ?? []);
    renderSummaryMetrics(contData.summary ?? {});
    lastUpdatedEl.textContent = `Last Updated: ${new Date().toLocaleTimeString()}`;
    setBackendUp(true);
  } catch (err) {
    setBackendUp(false);
    statusContainer.innerHTML =
      '<div class="panel"><div class="result-empty">WAITING FOR CIPHER AI BACKEND...</div></div>';
    continuationsContainer.innerHTML =
      '<div class="result-empty">WAITING FOR CIPHER AI BACKEND...</div>';
  }
}

updateCharCount();
pollDashboard();
setInterval(pollDashboard, 2000);
