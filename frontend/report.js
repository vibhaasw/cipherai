"use strict";

const statusEl = document.getElementById("report-status");
const contentEl = document.getElementById("report-content");

function esc(value) {
  const div = document.createElement("div");
  div.textContent = String(value ?? "");
  return div.innerHTML;
}

/** Minimal markdown renderer for our structured comparison doc (no external deps). */
function renderMarkdown(md) {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.startsWith("```")) {
      const fence = line.trim();
      i += 1;
      const codeLines = [];
      while (i < lines.length && !lines[i].startsWith("```")) {
        codeLines.push(lines[i]);
        i += 1;
      }
      i += 1;
      out.push(`<pre class="report-code">${esc(codeLines.join("\n"))}</pre>`);
      continue;
    }

    if (/^---+$/.test(line.trim())) {
      out.push("<hr>");
      i += 1;
      continue;
    }

    if (line.startsWith("### ")) {
      out.push(`<h3>${inlineFormat(line.slice(4))}</h3>`);
      i += 1;
      continue;
    }
    if (line.startsWith("## ")) {
      out.push(`<h2>${inlineFormat(line.slice(3))}</h2>`);
      i += 1;
      continue;
    }
    if (line.startsWith("# ")) {
      out.push(`<h1>${inlineFormat(line.slice(2))}</h1>`);
      i += 1;
      continue;
    }

    if (line.startsWith("> ")) {
      const quote = [];
      while (i < lines.length && lines[i].startsWith("> ")) {
        quote.push(lines[i].slice(2));
        i += 1;
      }
      out.push(`<blockquote>${inlineFormat(quote.join(" "))}</blockquote>`);
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && /^\|?[\s\-:|]+\|?$/.test(lines[i + 1].trim())) {
      const tableLines = [];
      while (i < lines.length && lines[i].includes("|")) {
        tableLines.push(lines[i]);
        i += 1;
      }
      out.push(renderTable(tableLines));
      continue;
    }

    if (line.trim() === "") {
      i += 1;
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim() !== "" && !lines[i].startsWith("#") && !lines[i].startsWith(">") && !lines[i].includes("|")) {
      para.push(lines[i]);
      i += 1;
    }
    out.push(`<p>${inlineFormat(para.join(" "))}</p>`);
  }

  return out.join("\n");
}

function inlineFormat(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function renderTable(tableLines) {
  if (tableLines.length < 2) return "";
  const parseRow = (row) =>
    row
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((c) => c.trim());

  const headers = parseRow(tableLines[0]);
  const bodyRows = tableLines.slice(2).map(parseRow);

  let html = "<table><thead><tr>";
  for (const h of headers) html += `<th>${inlineFormat(h)}</th>`;
  html += "</tr></thead><tbody>";
  for (const row of bodyRows) {
    html += "<tr>";
    for (const cell of row) html += `<td>${inlineFormat(cell)}</td>`;
    html += "</tr>";
  }
  html += "</tbody></table>";
  return html;
}

async function loadReport() {
  try {
    const res = await fetch("/docs/context-strategy-comparison");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const md = await res.text();
    statusEl.classList.add("hidden");
    contentEl.classList.remove("hidden");
    contentEl.innerHTML = renderMarkdown(md);
  } catch (err) {
    statusEl.innerHTML =
      `<div class="error-msg">Could not load comparison report: ${esc(err.message)}</div>`;
  }
}

loadReport();
