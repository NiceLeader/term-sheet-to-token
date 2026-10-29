// The issuance record of one bond as a document to read, print or file: what the token was
// issued from, with the page each term was read from and every decision of the reviewer.

const params = new URLSearchParams(location.search);
const run = params.get("run");
const bond = params.get("bond");
const $ = (sel) => document.querySelector(sel);
const enc = encodeURIComponent;

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `${response.status} for ${path}`);
  return response.json();
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }) + " UTC";
}

function show(value) {
  if (value === null || value === undefined || value === "") return "not stated";
  if (Array.isArray(value)) return value.map(show).join(", ");
  if (typeof value === "object") return Object.entries(value).map(([k, v]) => `${k}: ${show(v)}`).join("; ");
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

function modelName(model) {
  if (!model?.startsWith("muse-")) return model || "";
  return model.split("-").map((w) => (/^\d+b$/.test(w) ? w.toUpperCase()
    : /^[a-z]/.test(w) ? w[0].toUpperCase() + w.slice(1) : w)).join(" ");
}

function size(bytes) {
  return bytes == null ? "" : bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.round(bytes / 1000)} KB`;
}

function citations(list, titles) {
  if (!list?.length) return `<span class="where">no citation</span>`;
  return list.map((c) => `<div><span class="where">${c.role === "defined_in" ? "defined in" : "stated in"} ${esc(titles[c.doc] || c.doc)}${c.page ? `, p. ${c.page}` : ""}</span>${c.quote ? `<span class="quote">“${esc(c.quote)}”</span>` : ""}</div>`).join("");
}

function decision(entry) {
  if (!entry?.status) return `<span class="bad">not reviewed</span>`;
  const at = entry.at ? `, ${esc(when(entry.at))}` : "";
  if (entry.status === "corrected") {
    return `<span class="corrected">corrected</span> from <b>${esc(show(entry.read))}</b>${at}`;
  }
  return `<span class="ok">approved</span>${at}`;
}

function render(record, groups) {
  const titles = Object.fromEntries(record.documents.map((d) => [d.doc, d.title || d.doc]));
  const r = record.reading;
  const reading = r.model ? `${esc(modelName(r.model))}, ${r.mode === "one_pass" ? "in one pass" : "as an agent"}${r.seconds ? `, ${Math.round(r.seconds)} s` : ""}${r.cost_usd != null ? `, $${Number(r.cost_usd).toFixed(3)}` : ""} (run ${esc(r.run)})` : `${esc(r.source || "verified terms")} (${esc(r.run)})`;
  const s = record.replay?.summary || {};
  const t = record.replay?.token || {};
  const ids = [record.bond.isin && `ISIN ${record.bond.isin}`, record.bond.cusip && `CUSIP ${record.bond.cusip}`].filter(Boolean).join(", ");

  const terms = groups.map(({ group, fields }) => `
    <h2>${esc(group)}</h2>
    <table>
      <thead><tr><th>Term</th><th>Value</th><th>Read from</th><th>Reviewer</th></tr></thead>
      <tbody>${fields.map(({ name, label }) => `
        <tr>
          <td class="label">${esc(label)}</td>
          <td class="value">${esc(show(record.terms[name]))}</td>
          <td>${citations(record.citations[name], titles)}</td>
          <td>${decision(record.review[name])}</td>
        </tr>`).join("")}
      </tbody>
    </table>`).join("");

  const constructorTerms = t.config ? Object.entries(t.config).map(([k, v]) => `<tr><td class="label">${esc(k)}</td><td class="mono">${esc(show(v))}</td></tr>`).join("") : "";

  $("#record").innerHTML = `
    <div class="tools">
      <a class="button small ghost" href="/#/${enc(run)}/${enc(bond)}">Back to the review screen</a>
      <a class="button small ghost" href="/api/issued/${enc(run)}/${enc(bond)}" target="_blank" rel="noopener">Record as JSON</a>
      <button class="button small ghost" onclick="window.print()">Print</button>
    </div>
    <h1>Issuance record: ${esc(record.bond.label || record.bond.id)}</h1>
    <p class="sub">${esc(ids)}${ids ? ". " : ""}Issued ${esc(when(record.issued_at))}. Terms read by ${reading}.</p>
    <dl>
      <dt>Record SHA-256</dt><dd class="hash">${esc(record.sha256)}</dd>
      <dt>Token</dt><dd class="mono">${esc(t.bond || "")}</dd>
      <dt>Cash token</dt><dd class="mono">${esc(t.cash || "")}</dd>
    </dl>

    <h2>Documents</h2>
    <table>
      <thead><tr><th>Role</th><th>Document</th><th>Size</th><th>SHA-256</th></tr></thead>
      <tbody>${record.documents.map((d) => `
        <tr>
          <td class="label">${esc(d.role.replace(/_/g, " "))}</td>
          <td>${d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title || d.doc)}</a>` : esc(d.title || d.doc)}</td>
          <td>${esc(size(d.bytes))}</td>
          <td class="hash">${esc(d.sha256 || "")}</td>
        </tr>`).join("")}
      </tbody>
    </table>

    ${terms}

    <h2>Replay on a local chain</h2>
    <dl>
      <dt>Payments</dt><dd>${esc(s.payments ?? "")}${s.payments_ok ? `, <span class="ok">engine, QuantLib, token and the documents agree on every one</span>` : `, <span class="bad">disagreement</span>`}</dd>
      <dt>Guards</dt><dd>${esc(s.probes || "")} rejected as they must be</dd>
      <dt>Checks</dt><dd>${s.errors ? `<span class="bad">${s.errors} errors</span>` : "no errors"}${s.warnings ? `, ${s.warnings} warnings` : ""}</dd>
      <dt>Units</dt><dd>${esc(show(t.units))}${t.allocation ? ` (${esc(show(t.allocation))})` : ""}</dd>
      <dt>Deployment</dt><dd>${t.deploy_gas ? `${Number(t.deploy_gas).toLocaleString("en-GB")} gas` : ""}</dd>
    </dl>
    ${constructorTerms ? `<h2>Constructor arguments</h2><table><tbody>${constructorTerms}</tbody></table>` : ""}
    <p class="sub" style="margin-top:24px">A question about a payment goes back to the page, the reading and the reviewer. Term Sheet to Token.</p>`;
  document.title = `Issuance record: ${record.bond.label || record.bond.id}`;
}

async function main() {
  if (!run || !bond) {
    $("#record").innerHTML = `<div class="error">The address must name a run and a bond: record.html?run=…&amp;bond=…</div>`;
    return;
  }
  try {
    const [record, groups] = await Promise.all([api(`/api/issued/${enc(run)}/${enc(bond)}`), api("/api/fields")]);
    render(record, groups);
  } catch (error) {
    $("#record").innerHTML = `<div class="error">${esc(error.message)}</div>`;
  }
}

main();
