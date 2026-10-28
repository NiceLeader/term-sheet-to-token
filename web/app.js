"use strict";

// Review screen for one bond: terms and their sources, the cited page, the token's payments.

const $ = (sel) => document.querySelector(sel);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ROLES = {
  final_terms: "Final terms", pricing_supplement: "Pricing supplement",
  supplement: "Supplement", base_prospectus: "Base prospectus",
  prospectus_supplement: "Prospectus supplement", prospectus: "Prospectus",
  offering_circular: "Offering circular", offering_memorandum: "Offering memorandum",
  term_sheet: "Term sheet",
};
const CLAUSE_FIELDS = {
  coupon: ["interest_rate", "day_count_fraction", "business_day_convention", "business_day",
           "rounding_rule", "interest_on_holding", "record_date"],
  first: ["interest_commencement_date", "first_interest_payment_date"],
  redemption: ["final_redemption_amount", "maturity_date", "business_day_convention",
               "record_date"],
};
const ZERO_DECIMALS = new Set(["JPY", "KRW"]);
const NAMES = {
  ACT_ACT_ICMA: "Actual/Actual (ICMA)", THIRTY_360: "30/360", THIRTY_E_360: "30E/360",
  ACT_365_FIXED: "Actual/365 (Fixed)", ACT_360: "Actual/360", RBA_BOND_BASIS: "RBA Bond Basis",
  ACT_ACT_CANADIAN_COMPOUND: "Actual/Actual (Canadian compound)",
  FOLLOWING: "Following", MODIFIED_FOLLOWING: "Modified following", PRECEDING: "Preceding",
  NONE: "None (unadjusted)", HALF_UP: "Half up", HALF_EVEN: "Half even", DOWN: "Down",
  UP: "Up", NOT_STATED: "Not stated",
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const state = { runs: [], bonds: [], run: null, bond: null, data: null, documents: [],
                field: null, view: null, doc: null, page: 1, quotes: [], label: "",
                second: undefined };  // run id of the second reading, "none", or the default

const secondQuery = () => (state.second ? `?second=${encodeURIComponent(state.second)}` : "");

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

// ---------------------------------------------------------------- formatting

function amount(text) {
  if (text === null || text === undefined) return "";
  const [whole, fraction] = String(text).split(".");
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction !== undefined ? "." + fraction : "");
}

function minor(value, currency) {
  const decimals = ZERO_DECIMALS.has(currency) ? 0 : 2;
  return amount((value / 10 ** decimals).toFixed(decimals));
}

function shortDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00Z");
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

function modelName(model) {
  if (!model?.startsWith("muse-")) return model || "";
  // muse-spark-1.3-contributor -> Muse Spark 1.3 Contributor, muse-glimmer-30b -> Muse Glimmer 30B
  return model.split("-").map((w) => (/^\d+b$/.test(w) ? w.toUpperCase()
    : /^[a-z]/.test(w) ? w[0].toUpperCase() + w.slice(1) : w)).join(" ");
}

function runLabel(run) {
  if (run.kind === "truth") return "Verified terms";
  const where = run.model.startsWith("muse-glimmer") ? "local" : "Meta API";
  return `${modelName(run.model)} · ${run.mode.replace("_", "-")}${run.tag ? ` · ${run.tag}` : ""} (${where})`;
}

function fieldValue(field) {
  const v = field.value;
  if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return null;
  if (field.name === "stated_coupon_amounts") {
    return v.map((c) => `${amount(c.amount)} per ${amount(c.per)}${c.date ? " on " + shortDate(c.date) : ""}`).join("; ");
  }
  if (field.name === "interest_payment_dates") {
    return v.map((d) => { const [m, day] = String(d).split("-"); return `${Number(day)} ${MONTHS[Number(m) - 1] || m}`; }).join(", ");
  }
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (field.name.endsWith("_date") && /^\d{4}-\d{2}-\d{2}$/.test(v)) return shortDate(v);
  if (["aggregate_nominal_amount", "specified_denomination", "calculation_amount"].includes(field.name)) return amount(v);
  return NAMES[v] || String(v);
}

function fieldStatus(field, quoted) {
  if (field.flags.some((f) => f.severity === "error")) return ["bad", "!"];
  if (field.flags.some((f) => f.severity === "warning")) return ["warn", "?"];
  if (field.second && !field.second.agrees) return ["warn", "?"];
  if (!quoted) return field.cites.length ? ["none", "p"] : ["none", ""];
  const stated = field.cites.filter((c) => c.role === "stated_in");
  if (field.value === null || field.value === undefined) return ["none", ""];
  if (stated.length && stated.every((c) => c.status === "exact" || c.status === "fuzzy")
      && field.value_on_page !== false) return ["ok", "✓"];
  return ["warn", "?"];
}

const citeStatus = {
  exact: ["ok", "quote on page"], fuzzy: ["ok", "quote on page (near match)"],
  wrong_page: ["warn", "quote on another page"], wrong_doc: ["warn", "quote in another document"],
  not_found: ["bad", "quote not found"], empty: ["bad", "no quote"], cited: ["", "cited page"],
};

// ---------------------------------------------------------------- loading

async function init() {
  [state.runs, state.bonds] = await Promise.all([api("/api/runs"), api("/api/bonds")]);
  renderBondPicker();
  $("#bond").addEventListener("change", () => go(state.run || "groundtruth", $("#bond").value));
  document.addEventListener("keydown", reviewKeys);
  document.querySelectorAll("[data-left]").forEach((tab) => tab.addEventListener("click", () => leftTab(tab.dataset.left)));
  $("#prev").addEventListener("click", () => openPage(state.doc, state.page - 1, [], ""));
  $("#next").addEventListener("click", () => openPage(state.doc, state.page + 1, [], ""));
  $("#page").addEventListener("change", () => openPage(state.doc, Number($("#page").value), [], ""));
  $("#find").addEventListener("submit", (e) => { e.preventDefault(); find($("#find-q").value); });
  document.querySelectorAll("#extract button").forEach((b) => b.addEventListener("click", () => startExtraction(b.dataset.kind)));
  window.addEventListener("hashchange", route);
  route();
}

function renderBondPicker() {
  $("#bond").innerHTML = state.bonds.map((b) => `<option value="${esc(b.id)}">${esc(b.label)}${b.verified ? " ✓" : ""}</option>`).join("");
  if (state.bond) $("#bond").value = state.bond;
}

function go(run, bond) { location.hash = `#/bond/${run}/${bond}`; }

function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  if (parts[0] === "results") return showResults();
  $("#results-view").classList.add("hidden");
  ["#bond-view", "#sources", "#status", "#extract"].forEach((s) => $(s).classList.remove("hidden"));
  const [run, bond, field] = parts[0] === "bond" ? parts.slice(1, 4) : ["groundtruth", state.bonds[0]?.id];
  if (run === state.run && bond === state.bond && state.data) {
    if (field) selectField(field);
    return;
  }
  loadBond(run, bond, field);
}

async function loadBond(run, bond, field) {
  if (run !== state.run) state.second = undefined;  // the second reading is chosen per run
  state.run = run; state.bond = bond; state.field = null; state.doc = null;
  $("#bond").value = bond;
  renderSources();
  try {
    state.data = await api(`/api/bond/${run}/${bond}${secondQuery()}`);
    state.documents = state.data.documents;
  } catch (error) {
    state.data = null;
    const label = runLabel(state.runs.find((r) => r.id === run) || { kind: "truth" });
    const missing = /^no (extraction|ground truth)/.test(error.message);
    $("#terms").innerHTML = `<p class="empty-note">${missing ? `${esc(label)} has not read this bond yet. Use “Read again with” above to read it now.` : esc(error.message)}</p>`;
    $("#payments").innerHTML = `<p class="empty-note">No terms, so no token yet.</p>`;
    $("#trace").innerHTML = "";
    $("#status").innerHTML = "";
    $("#review-bar").innerHTML = "";
    $("#second").innerHTML = "";
    $("#model").textContent = "";
    $("#token").textContent = "";
    $("#cite-bar").classList.remove("show");
    $("#trace-tab").classList.add("hidden");
    leftTab("terms");
    // The documents can still be read, and followed while a model reads them.
    state.documents = await api(`/api/documents/${bond}`).catch(() => []);
    if (state.bond !== bond) return;
    renderDocTabs();
    if (state.documents.length) { openPage(state.documents[0].doc, 1, [], ""); return; }
    $("#page").value = ""; $("#pages").textContent = "";
    $("#sheet").innerHTML = `<div class="empty">No terms from this source for this bond yet.</div>`;
    return;
  }
  renderStatus();
  renderTerms();
  renderTrace();
  renderPayments();
  renderDocTabs();
  const first = state.documents[0];
  if (field) selectField(field); else openPage(first.doc, 1, [], "");
}

function renderSources() {
  $("#sources").innerHTML = state.runs.map((r) => `<button role="tab" data-run="${esc(r.id)}" class="${r.id === state.run ? "active" : ""}">${esc(runLabel(r))}</button>`).join("");
  $("#sources").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => go(b.dataset.run, state.bond)));
}

function renderStatus() {
  const report = state.data.report;
  const pills = [];
  if (report) {
    const s = report.summary;
    pills.push(s.payments_ok ? `<span class="pill ok">${s.payments} payments agree three ways</span>`
                             : `<span class="pill bad">payment mismatch</span>`);
    const [passed, total] = s.probes.split("/");
    pills.push(`<span class="pill ${passed === total ? "ok" : "bad"}">guards ${s.probes}</span>`);
    if (s.errors) pills.push(`<span class="pill bad">${s.errors} red flag${s.errors > 1 ? "s" : ""}</span>`);
    if (s.warnings) pills.push(`<span class="pill warn">${s.warnings} to review</span>`);
  } else {
    pills.push(`<span class="pill">not verified yet</span>`);
  }
  const score = state.data.score;
  if (score) pills.push(`<span class="pill ${score.fields_correct === score.fields_total ? "ok" : "warn"}" title="The model's own reading against the verified terms, from the last tstt eval">model read ${score.fields_correct}/${score.fields_total} fields right</span>`);
  $("#status").innerHTML = pills.join("");
  const x = state.data.extraction;
  $("#model").innerHTML = x ? `Read by <b>${esc(modelName(x.model))}</b><br>${esc(x.mode.replace("_", "-"))}, ${Math.round(x.seconds)} s${x.cost_usd != null ? `, $${x.cost_usd.toFixed(3)}` : ""}`
                            : `<b>Verified by hand</b><br>page references`;
  $("#trace-tab").classList.toggle("hidden", !x || x.mode !== "agent");
  leftTab("terms");
  renderSecondBar();
  renderReviewBar();
}

// A second reading of the same documents, by another model or in another mode: the terms
// where the two readings differ are marked for the reviewer, the others agree.
function renderSecondBar() {
  const s = state.data.second;
  if (!s || !(s.run || s.choices.length)) { $("#second").innerHTML = ""; return; }
  const options = [`<option value="none" ${s.run ? "" : "selected"}>none</option>`]
    .concat(s.choices.map((c) => `<option value="${esc(c.id)}" ${c.id === s.run ? "selected" : ""}>${esc(secondLabel(c.label))}</option>`)).join("");
  const differ = state.data.fields.filter((f) => f.second && !f.second.agrees).length;
  const chip = !s.run ? "" : differ ? `<span class="chip warn">${differ} term${differ === 1 ? "" : "s"} differ</span>`
                                    : `<span class="chip ok">all terms agree</span>`;
  // The reading chosen for an earlier bond may not have read this one: say so.
  const unmet = state.second && state.second !== "none" && state.second !== s.run
    ? `<span class="note">The reading chosen before has not read this bond.</span>` : "";
  $("#second").innerHTML = `<label>Second reading <select id="second-run" aria-label="Second reading to compare with">${options}</select></label>${chip}${unmet}`;
  $("#second-run").addEventListener("change", (e) => { state.second = e.target.value; refresh(state.field); });
}

function secondLabel(label) {
  // "muse-glimmer-30b, agent" -> "Muse Glimmer 30B, agent"
  const [model, mode] = label.split(", ");
  return `${modelName(model)}${mode ? `, ${mode}` : ""}`;
}

// ---------------------------------------------------------------- review

const truthRun = () => state.run === "groundtruth";

function renderReviewBar() {
  const r = state.data.review;
  const done = r.reviewed === r.total;
  const fixes = r.corrected ? `, ${r.corrected} corrected` : "";
  let html;
  if (r.issued) {
    html = `<span class="chip ok">${truthRun() ? "verified" : "issued"}</span>
      <span>${truthRun() ? "Every term checked against its page and replayed on chain" : `Token issued after review${fixes}`}${r.issued.at ? `, ${esc(shortDate(r.issued.at.slice(0, 10)))}` : ""}.</span>`;
  } else {
    html = `<span class="progress"><span style="width:${(r.reviewed / r.total) * 100}%"></span></span>
      <span title="Keys: A approves the selected term and moves to the next one, J and K move down and up">Reviewed <b>${r.reviewed}</b> of ${r.total}${fixes}</span>
      <button class="button" id="issue" ${done ? "" : "disabled"} title="${done ? "" : "Approve or correct every term first"}">${truthRun() ? "Mark verified" : "Issue token"}</button>`;
  }
  $("#review-bar").innerHTML = html + `<div class="review-msg" id="review-msg"></div>`;
  $("#issue")?.addEventListener("click", issueToken);
}

async function issueToken(event) {
  const button = event.target;
  button.disabled = true; button.textContent = "Deploying and paying on a local chain…";
  try {
    const result = await api(`/api/issue/${state.run}/${state.bond}`, { method: "POST" });
    if (result.issued && truthRun()) { state.bonds = await api("/api/bonds"); renderBondPicker(); }
    await refresh(state.field);
    if (!result.issued) {
      const s = result.summary;
      $("#review-msg").textContent = `Not issued: ${s.errors} red flag${s.errors === 1 ? "" : "s"}${s.payments_ok ? "" : ", payment mismatch"}. See “Before issuance” on the right.`;
    }
  } catch (error) {
    $("#review-msg").textContent = error.message;
    button.disabled = false; button.textContent = truthRun() ? "Mark verified" : "Issue token";
  }
}

async function refresh(field) {
  const data = await api(`/api/bond/${state.run}/${state.bond}${secondQuery()}`);
  state.data = data;
  renderStatus();
  renderTerms();
  renderPayments();
  if (field) selectField(field);
}

async function review(name, action, value) {
  const error = document.querySelector(`.field[data-name="${CSS.escape(name)}"] .review-error`);
  try {
    await api(`/api/review/${state.run}/${state.bond}/${name}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, value: value ?? null }),
    });
  } catch (e) {
    if (error) error.textContent = e.message;
    return false;
  }
  const next = action === "reset" ? name : nextUnreviewed(name) || name;
  await refresh(next);
  return true;
}

function nextUnreviewed(after) {
  const fields = state.data.fields;
  const at = fields.findIndex((f) => f.name === after);
  const rest = [...fields.slice(at + 1), ...fields.slice(0, at)];
  return rest.find((f) => !f.review)?.name;
}

function reviewKeys(event) {
  if (!state.data || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.closest("input, select, textarea")) return;
  if ($("#bond-view").classList.contains("hidden") || $("#terms").classList.contains("hidden")) return;
  const fields = state.data.fields;
  const at = fields.findIndex((f) => f.name === state.field);
  const key = event.key.toLowerCase();
  if (key === "j" || key === "k") {
    const next = fields[Math.min(Math.max(at + (key === "j" ? 1 : -1), 0), fields.length - 1)];
    if (next) selectField(next.name);
  } else if (key === "a" && state.field) {
    const f = fields[at];
    if (!f.review) review(f.name, "approve");
    else { const next = nextUnreviewed(f.name); if (next) selectField(next); }
  } else {
    return;
  }
  event.preventDefault();
}

// Corrections are typed as text and turned into the field's JSON shape; the server
// validates them with the same rules as the model's output.
function inputFor(f) {
  const raw = f.value;
  if (f.kind === "enum" || f.kind === "bool") {
    const options = f.kind === "bool" ? [["true", "yes"], ["false", "no"]]
                                      : f.options.map((o) => [o, NAMES[o] || o]);
    const current = raw === null || raw === undefined ? "" : String(raw);
    return `<select name="v"><option value="">not stated</option>${options.map(([v, label]) => `<option value="${esc(v)}" ${v === current ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
  }
  if (f.kind === "date") return `<input name="v" type="date" value="${esc(raw || "")}">`;
  let text = raw ?? "";
  if (f.kind === "list") text = (raw || []).join(", ");
  if (f.kind === "coupons") text = (raw || []).map((c) => `${c.amount} per ${c.per}${c.date ? " on " + c.date : ""}`).join("; ");
  const hint = { list: f.name === "interest_payment_dates" ? "MM-DD, MM-DD" : "London, New York",
                 coupons: "7.50 per 1000; 6.45 per 1000 on 2020-12-15" }[f.kind] || "";
  return `<input name="v" value="${esc(text)}" placeholder="${esc(hint)}" autocomplete="off">`;
}

function valueFrom(f, text) {
  text = text.trim();
  if (!text) return null;
  if (f.kind === "bool") return text === "true";
  if (f.kind === "int") {
    if (!/^\d+$/.test(text)) throw new Error(`write a whole number of months, such as 6: ${text}`);
    return Number(text);
  }
  if (f.kind === "list") return text.split(",").map((s) => s.trim()).filter(Boolean);
  if (f.kind === "coupons") {
    return text.split(/[;\n]/).map((s) => s.trim()).filter(Boolean).map((s) => {
      const m = s.match(/^([\d.,]+)\s+per\s+([\d.,]+)(?:\s+on\s+(\d{4}-\d{2}-\d{2}))?$/i);
      if (!m) throw new Error(`write each amount as “7.50 per 1000” or “6.45 per 1000 on 2020-12-15”: ${s}`);
      return { amount: m[1].replace(/,/g, ""), per: m[2].replace(/,/g, ""), date: m[3] || null };
    });
  }
  return text;
}

function reviewHtml(f) {
  const who = truthRun() ? "Draft value" : "Model read";
  const original = f.review === "corrected"
    ? `<div class="note">${who}: <s>${esc(fieldValue({ ...f, value: f.original }) ?? "not stated")}</s></div>` : "";
  const status = { approved: "Approved", corrected: "Corrected" }[f.review];
  return `<div class="review-controls">${original}
      ${status ? `<span class="chip ${f.review === "approved" ? "ok" : "warn"}">${status}</span>` : ""}
      ${f.review !== "corrected" && f.review !== "approved" ? `<button class="button small" data-act="approve">Approve</button>` : ""}
      <button class="button small ghost" data-act="edit">Correct…</button>
      ${f.review ? `<button class="button small ghost" data-act="reset">Undo</button>` : ""}
    </div>
    <form class="correct hidden">${inputFor(f)}<button class="button small">Save</button><button type="button" class="button small ghost" data-act="cancel">Cancel</button></form>
    <div class="review-error"></div>`;
}

function bindReview(el, f) {
  const form = el.querySelector("form.correct");
  el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", (event) => {
    event.stopPropagation();
    const act = b.dataset.act;
    el.querySelector(".review-error").textContent = "";
    if (act === "edit") { form.classList.remove("hidden"); form.elements.v.focus(); }
    else if (act === "cancel") form.classList.add("hidden");
    else if (act === "take") review(f.name, "correct", f.second.value);  // the second reading's value
    else review(f.name, act);
  }));
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    let value;
    try { value = valueFrom(f, form.elements.v.value); }
    catch (e) { el.querySelector(".review-error").textContent = e.message; return; }
    review(f.name, "correct", value);
  });
  form?.addEventListener("click", (event) => event.stopPropagation());
}

function leftTab(which) {
  document.querySelectorAll("[data-left]").forEach((t) => t.classList.toggle("active", t.dataset.left === which));
  $("#terms").classList.toggle("hidden", which !== "terms");
  $("#trace").classList.toggle("hidden", which !== "trace");
}

// ---------------------------------------------------------------- terms

function renderTerms() {
  const quoted = Boolean(state.data.extraction);
  let html = "", group = null;
  for (const f of state.data.fields) {
    if (f.group !== group) { group = f.group; html += `<h3 class="group-title">${esc(group)}</h3>`; }
    const value = fieldValue(f);
    const [cls, sym] = f.review === "corrected" ? ["fix", "✎"] : fieldStatus(f, quoted);
    const pageRef = !quoted && f.cites.length ? `p.${f.cites[0].page}` : sym;
    const cites = f.cites.map((c, i) => {
      const [sc, st] = citeStatus[c.status] || ["", c.status];
      const doc = state.documents.find((d) => d.doc === c.doc);
      const where = `${esc(ROLES[doc?.role] || c.doc)}, p.${esc(c.page)}`;
      const moved = c.found_page && c.found_page !== c.page ? ` (found on p.${esc(c.found_page)})` : "";
      return `<div class="cite" data-cite="${i}"><span class="where">${c.role === "defined_in" ? "Defined in" : "Stated in"} ${where}
        ${st ? `<span class="chip ${sc}">${esc(st)}${moved}</span>` : ""}</span>${c.quote ? `<q>${esc(c.quote)}</q>` : ""}</div>`;
    }).join("");
    const onPage = f.value_on_page === false ? `<div class="flag-line warning">The value is not written in the quotes or on the cited pages.</div>` : "";
    const flags = f.flags.map((x) => `<div class="flag-line ${esc(x.severity)}">${esc(x.message)}</div>`).join("");
    const second = !f.second ? "" : f.second.agrees
      ? `<div class="note">The second reading (${esc(secondLabel(state.data.second.label))}) agrees.</div>`
      : `<div class="flag-line warning">The second reading (${esc(secondLabel(state.data.second.label))}) gives: ${esc(fieldValue({ name: f.name, value: f.second.value }) ?? "not stated")}
          ${f.second.cite ? `<a class="second-cite" href="#" data-doc="${esc(f.second.cite.doc)}" data-page="${esc(f.second.cite.page)}" data-quote="${esc(f.second.cite.quote ?? "")}">${esc(ROLES[state.documents.find((d) => d.doc === f.second.cite.doc)?.role] || f.second.cite.doc)}, p.${esc(f.second.cite.page)}</a>` : ""}
          ${f.second.value != null && f.review !== "corrected" ? `<button class="button small ghost" data-act="take">Use this value</button>` : ""}</div>`;
    const reviewed = f.review ? `<span class="rv ${f.review}" title="${f.review}">${f.review === "approved" ? "✓ approved" : "corrected"}</span>` : "";
    html += `<div class="field ${f.review ? "reviewed" : ""}" data-name="${esc(f.name)}">
      <span class="label">${esc(f.label)}${reviewed}</span>
      <span class="mark"><span class="dot ${cls}" title="${esc(cls)}">${esc(pageRef)}</span></span>
      <span class="value ${value === null ? "empty" : ""}">${esc(value ?? "not stated")}</span>
      <div class="detail">${reviewHtml(f)}${flags}${second}${onPage}${cites || `<div class="note">No source cited.</div>`}
        ${f.note ? `<div class="note">${esc(f.note)}</div>` : ""}
        ${f.bdt ? `<div class="note">ICMA Bond Data Taxonomy: ${esc(f.bdt)}</div>` : ""}</div>
    </div>`;
  }
  $("#terms").innerHTML = html;
  $("#terms").querySelectorAll(".field").forEach((el) => {
    bindReview(el, state.data.fields.find((f) => f.name === el.dataset.name));
    el.addEventListener("click", (event) => {
      const other = event.target.closest(".second-cite");
      if (other) {  // the page the second reading cites, with its quote marked
        event.preventDefault();
        highlightField(el.dataset.name);
        openPage(other.dataset.doc, Number(other.dataset.page), other.dataset.quote ? [other.dataset.quote] : [],
                 `${state.data.fields.find((f) => f.name === el.dataset.name).label} (second reading)`);
        return;
      }
      const cite = event.target.closest(".cite");
      selectField(el.dataset.name, cite ? Number(cite.dataset.cite) : 0);
    });
  });
}

function highlightField(name) {
  state.field = name;
  leftTab("terms");
  document.querySelectorAll(".field").forEach((el) => el.classList.toggle("selected", el.dataset.name === name));
  const row = document.querySelector(`.field[data-name="${CSS.escape(name)}"]`);
  row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function selectField(name, citeIndex = 0) {
  const field = state.data?.fields.find((f) => f.name === name);
  if (!field) return;
  highlightField(name);
  const cite = field.cites[citeIndex] || field.cites[0];
  if (!cite) {
    if (!state.doc) openPage(state.documents[0].doc, 1, [], "");
    return;
  }
  const page = cite.found_page && cite.status === "wrong_page" ? cite.found_page : cite.page;
  const quoted = field.cites.filter((c) => c.doc === cite.doc && c.quote);
  openPage(cite.doc, Number(page), quoted.map((c) => c.quote), field.label,
           quoted.map((c) => c.status));
}

// ---------------------------------------------------------------- document

function renderDocTabs() {
  $("#doc-tabs").innerHTML = state.documents.map((d) => `<button class="tab" data-doc="${esc(d.doc)}" title="${esc(d.title)}">${esc(ROLES[d.role] || d.role)}<small>${d.pages} p.</small></button>`).join("");
  $("#doc-tabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => openPage(b.dataset.doc, 1, [], "")));
}

// `checked` holds the quote check's verdict for each quote, when the quotes come from a field.
async function openPage(doc, page, quotes, label, checked = null) {
  const meta = state.documents.find((d) => d.doc === doc);
  if (!meta) return;
  page = Math.min(Math.max(1, page || 1), meta.pages);
  Object.assign(state, { doc, page, quotes, label });
  document.querySelectorAll("#doc-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.doc === doc));
  $("#page").value = page; $("#page").max = meta.pages; $("#pages").textContent = `of ${meta.pages}`;
  const params = new URLSearchParams();
  quotes.forEach((q) => params.append("q", q));
  const view = await api(`/api/view/${doc}/${page}?${params}`);
  if (state.doc !== doc || state.page !== page) return; // a newer request won
  state.view = view;
  const sheet = $("#sheet");
  if (view.format === "pdf") {
    sheet.innerHTML = `<img alt="${esc(meta.title)}, page ${page}" src="${esc(view.image)}">` +
      view.marks.flatMap((m) => m.boxes).map((b) => `<div class="hl" style="left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%"></div>`).join("");
  } else {
    let text = "", at = 0;
    const spans = view.marks.flatMap((m) => m.spans).sort((a, b) => a[0] - b[0]);
    for (const [s, e] of spans) {
      if (s < at) continue;
      text += esc(view.text.slice(at, s)) + `<mark>${esc(view.text.slice(s, e))}</mark>`;
      at = e;
    }
    sheet.innerHTML = `<pre>${text + esc(view.text.slice(at))}</pre>`;
  }
  const marked = view.marks.map((m) => (m.boxes || m.spans).length > 0);
  const found = marked.filter(Boolean).length;
  // The page marks the closest passage; the quote check is stricter (every number must match).
  const loose = checked ? marked.filter((m, i) => m && !["exact", "fuzzy", "wrong_page"].includes(checked[i])).length : 0;
  const bar = $("#cite-bar");
  if (label) {
    bar.innerHTML = `<b>${esc(label)}</b>: ${esc(meta.title)}, page ${page}. ` +
      (quotes.length ? `${found} of ${quotes.length} quote${quotes.length > 1 ? "s" : ""} located and marked.` : "Page cited, no quote.") +
      (loose ? ` <span class="chip bad">${loose === 1 ? "The marked passage is" : `${loose} marked passages are`} only the closest text: the quote is not on this page word for word.</span>` : "");
    bar.classList.add("show");
  } else {
    bar.classList.remove("show");
  }
  const scrollTo = () => {
    const mark = sheet.querySelector(".hl, mark");
    if (mark) mark.scrollIntoView({ block: "center", behavior: "smooth" });
    else $("#page-wrap").scrollTop = 0;
  };
  const img = sheet.querySelector("img");
  if (img && !img.complete) img.addEventListener("load", scrollTo, { once: true }); else scrollTo();
}

async function find(phrase) {
  if (!phrase.trim() || !state.doc) return;
  const hits = await api(`/api/search/${state.doc}?q=${encodeURIComponent(phrase)}`);
  const bar = $("#cite-bar");
  bar.innerHTML = `<b>${esc(phrase)}</b>: ${hits.length ? `${hits.length} page${hits.length > 1 ? "s" : ""}` : "not found"} in this document` +
    `<div class="hits">${hits.map((p) => `<button data-p="${p}">p.${p}</button>`).join("")}</div>`;
  bar.classList.add("show");
  bar.querySelectorAll("button").forEach((b) => b.addEventListener("click", async () => {
    await openPage(state.doc, Number(b.dataset.p), [phrase], `Search "${phrase}"`);
  }));
}

// ---------------------------------------------------------------- agent trace

function docTitle(doc) {
  return ROLES[state.documents.find((d) => d.doc === doc)?.role] || doc || "all documents";
}

function stepHtml(s, fresh = false) {
  let what = "", link = "";
  const args = s.args || {};
  if (s.tool === "search") what = `“${esc(args.query)}” in ${esc(docTitle(args.doc))}`;
  else if (s.tool === "read_page") { what = `${esc(docTitle(args.doc))}, page ${esc(args.page)}`; link = `data-doc="${esc(args.doc)}" data-page="${esc(args.page)}"`; }
  else if (s.tool === "list_documents") what = "documents of this bond";
  else if (s.tool === "submit_terms") what = s.errors?.length ? `${s.errors.length} problem(s) returned to the model` : "accepted";
  else what = "answered in prose, sent back to the tools";
  if (s.repeat_of) what += ` <span class="repeat">same as step ${esc(s.repeat_of)}, not run again</span>`;
  const tool = (s.tool || "reply").replace("_", " ");
  return `<div class="step ${link ? "link" : ""} ${s.tool === "submit_terms" ? "submit" : ""} ${fresh ? "fresh" : ""}" ${link}><span class="n">${esc(s.step)}</span><span><span class="tool">${esc(tool)}</span> <span class="args">${what}</span></span></div>`;
}

function bindSteps() {
  $("#trace").querySelectorAll(".step.link").forEach((el) => el.addEventListener("click", () => openPage(el.dataset.doc, Number(el.dataset.page), [], "Page read by the model")));
}

function renderTrace() {
  const x = state.data.extraction;
  if (!x || x.mode !== "agent") { $("#trace").innerHTML = ""; return; }
  const reads = x.tool_log.filter((s) => s.tool === "read_page").length;
  const searches = x.tool_log.filter((s) => s.tool === "search").length;
  $("#trace").innerHTML = `<div class="trace-summary"><b>${esc(modelName(x.model))}</b> worked through the documents with four tools:
    ${searches} searches, ${reads} page reads, then ${x.error ? "stopped: " + esc(x.error) + "." : "submitted the terms."} ${x.steps} steps,
    ${Math.round(x.seconds)} s, ${amount(String(x.usage.input_tokens))} tokens read, ${amount(String(x.usage.output_tokens))} written.</div>` +
    x.tool_log.map((s) => stepHtml(s)).join("");
  bindSteps();
}

// ---------------------------------------------------------------- live extraction

async function startExtraction(kind) {
  const buttons = document.querySelectorAll("#extract button, #extract select");
  buttons.forEach((b) => { b.disabled = true; });
  const bond = state.bond;
  const mode = kind === "hosted" ? $("#mode").value : "agent";
  const name = kind === "hosted" ? "Muse Spark" : "Muse Glimmer";
  const earlier = $("#trace").innerHTML;
  $("#trace-tab").classList.remove("hidden");
  leftTab("trace");
  $("#trace").innerHTML = `<div class="live" id="live"><span class="spinner"></span><span id="live-text"><b>${name}</b> is reading the documents of this bond…</span> <small id="live-time"></small></div><div id="live-steps"></div>`;
  const live = $("#live");
  const began = Date.now();
  const tick = setInterval(() => {
    const time = $("#live-time");
    if (time) time.textContent = `${Math.round((Date.now() - began) / 1000)} s`;
  }, 1000);
  const finish = () => { clearInterval(tick); buttons.forEach((b) => { b.disabled = false; }); };
  let replaced = null; // the run whose reading of this bond the new one has overwritten
  const fail = async (message) => {
    finish();
    if (replaced) state.runs = await api("/api/runs").catch(() => state.runs);
    if (replaced && state.bond === bond && state.run === replaced) {
      // The earlier reading is gone, so the failed one is shown, with what it got to.
      await loadBond(replaced, bond, state.field);
      $("#trace").insertAdjacentHTML("afterbegin", `<div class="live error">${esc(message)}</div>`);
      $("#trace-tab").classList.remove("hidden");
      leftTab("trace");
    } else if (live.isConnected) {
      live.className = "live error"; live.textContent = message;
      if (earlier) { $("#live-steps").insertAdjacentHTML("afterend", `<h3 class="section-title">Earlier reading</h3>${earlier}`); bindSteps(); }
      if (replaced) renderSources();
    } else {
      // The reviewer has moved on; the header still says that the reading failed.
      if (replaced) renderSources();
      const label = state.bonds.find((b) => b.id === bond)?.label || bond;
      $("#status").insertAdjacentHTML("afterbegin", `<span class="pill bad" title="${esc(message)}">reading ${esc(label)} again failed</span>`);
    }
  };
  let job;
  try {
    job = (await api(`/api/extract/${kind}/${bond}?mode=${mode}`, { method: "POST" })).job;
  } catch (error) {
    fail(error.message); return;
  }
  let model = name;
  const events = new EventSource(`/api/jobs/${job}/events`);
  events.onmessage = (message) => {
    const e = JSON.parse(message.data);
    // The live block is gone once the reviewer opens another bond or source.
    const text = $("#live-text"), steps = $("#live-steps");
    if (e.type === "start") {
      model = modelName(e.model);
      if (text) text.innerHTML = `<b>${esc(model)}</b> is reading the documents of this bond${e.mode === "one_pass" ? " in one pass" : ""}…`;
    } else if (e.type === "step") {
      if (steps) {
        steps.insertAdjacentHTML("beforeend", stepHtml(e, true));
        steps.lastElementChild.scrollIntoView({ block: "nearest" });
        bindSteps();
      }
      if (text) text.innerHTML = `<b>${esc(model)}</b>: step ${e.step}, ${amount(String(e.input_tokens))} tokens read`;
      if (e.tool === "read_page" && state.bond === bond) openPage(e.args.doc, Number(e.args.page), [], "Page the model is reading");
    } else if (e.type === "saved") {
      replaced = e.run;
    } else if (e.type === "extracted") {
      const how = mode === "agent" ? `after ${e.steps} step${e.steps === 1 ? "" : "s"}` : `in ${Math.round(e.seconds)} s`;
      if (text) text.textContent = `Terms submitted ${how}. Deploying the token and paying every coupon on a local chain…`;
    } else if (e.type === "done") {
      events.close(); finish();
      live.className = "live done";
      live.textContent = `Done: ${e.summary.payments} payments, guards ${e.summary.probes}, ${e.summary.errors} red flag${e.summary.errors === 1 ? "" : "s"}, ${e.summary.warnings} to review.`;
      api("/api/runs").then((runs) => {
        state.runs = runs;
        if (state.bond !== bond) renderSources();
        else if (location.hash.startsWith(`#/bond/${e.run}/${bond}`)) loadBond(e.run, bond, state.field);
        else go(e.run, bond);
      });
    } else if (e.type === "error") {
      events.close(); fail(e.message);
    }
  };
  events.onerror = () => { if (events.readyState === EventSource.CLOSED) finish(); };
}

// ---------------------------------------------------------------- payments

function renderPayments() {
  const report = state.data.report;
  if (!report) {
    $("#token").textContent = "";
    $("#payments").innerHTML = `<p class="empty-note">${state.data.review.corrected && !truthRun() ? "The corrected terms" : "These terms"} have not been deployed yet.</p>
      <p style="padding:0 14px"><button class="button" id="verify-now">Deploy the token and run every payment</button></p>`;
    $("#verify-now").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "Deploying and paying on a local chain…";
      try { await api(`/api/verify/${state.run}/${state.bond}`, { method: "POST" }); await refresh(state.field); }
      catch (error) { e.target.textContent = error.message; }
    });
    return;
  }
  const t = report.token;
  $("#token").innerHTML = `BondToken <code title="${esc(t.bond)}">${esc(t.bond.slice(0, 8))}…${esc(t.bond.slice(-4))}</code><br>${amount(String(t.units))} units, deployed for ${amount(String(t.deploy_gas))} gas`;
  const cur = report.currency;
  const total = Object.values(t.allocation).reduce((a, b) => a + b, 0);
  const items = [
    ...report.rows.map((r) => ({ date: r.executed, order: 1, row: r })),
    ...report.transfers.map((x) => ({ date: x.on, order: 0, transfer: x })),
  ].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.order - b.order));
  let html = `<div class="timeline"><div class="event">Issued ${amount(String(total))} units on ${shortDate(report.terms.issue_date)}: A ${share(t.allocation.A, total)}, B ${share(t.allocation.B, total)}, C ${share(t.allocation.C, total)}</div>`;
  let coupon = 0;
  for (const item of items) {
    if (item.transfer) {
      const x = item.transfer;
      html += `<div class="event">${shortDate(x.on)}: ${esc(x.from)} sells ${amount(String(x.units))} units to ${esc(x.to)}, ${esc(x.note)}</div>`;
      continue;
    }
    const r = item.row;
    const isCoupon = r.kind === "coupon";
    if (isCoupon) coupon += 1;
    const per = amount(r.per);
    const checks = [
      ["Engine", r.engine, true],
      ["QuantLib", r.quantlib, r.quantlib === r.engine],
      ["Token", r.chain, r.chain === r.engine],
      ["Printed", r.stated ? r.stated.split(" per ")[0] : null, r.stated ? r.problems.every((p) => !p.startsWith("printed")) : null],
    ].map(([name, value, ok]) => `<div class="check ${value == null ? "" : ok ? "ok" : "bad"}"><span>${name}</span><b>${value == null ? "–" : amount(value)}</b></div>`).join("");
    const holders = Object.entries(r.holders_paid).sort();
    const units = holders.reduce((a, [, [u]]) => a + u, 0) || 1;
    const bar = holders.map(([h, [u]]) => `<div class="h-${esc(h)}" style="width:${(u / units) * 100}%">${esc(h)}</div>`).join("");
    const legend = holders.map(([h, [u, paid]]) => `<span>${esc(h)}: ${amount(String(u))} units, ${cur} ${minor(paid, cur)}</span>`).join("");
    const deps = [...(isCoupon ? CLAUSE_FIELDS.coupon : CLAUSE_FIELDS.redemption), ...(isCoupon && coupon === 1 ? CLAUSE_FIELDS.first : [])];
    const depChips = deps.map((name) => {
      const f = state.data.fields.find((x) => x.name === name);
      return f && f.value != null && f.value !== false ? `<button data-field="${esc(name)}">${esc(f.label)}</button>` : "";
    }).join("");
    html += `<div class="pay ${esc(r.kind)} ${r.status === "ok" ? "" : "mismatch"}">
      <div class="pay-top"><span class="pay-kind">${isCoupon ? `Coupon ${coupon}` : "Redemption"}</span>
        <span class="pay-date">paid ${shortDate(r.executed)}${r.executed !== r.scheduled ? ` (due ${shortDate(r.scheduled)})` : ""} · record ${shortDate(r.record_date)}</span></div>
      <div class="pay-amount">${cur} ${amount(r.engine)} <small>per ${per}${r.accrual ? ` · interest ${r.accrual.split(" to ").map(shortDate).join(" – ")}` : ""}${r.on_holding ? " · each holder paid on the whole holding" : ""}</small></div>
      <div class="checks">${checks}</div>
      <div class="holders" title="Holders of record">${bar}</div>
      <div class="holders-legend">${legend}</div>
      <div class="deps">${depChips}</div>
      ${r.problems.length ? `<div class="problems">${r.problems.map(esc).join("<br>")}</div>` : ""}
    </div>`;
  }
  html += `</div><h3 class="section-title">Guards tested on chain</h3>` +
    report.probes.map((p) => `<div class="probe"><span>${esc(p.name)}</span><span class="chip ${p.passed ? "ok" : "bad"}">${p.passed ? "rejected: " : "FAILED: "}${esc(p.actual ?? "accepted")}</span></div>`).join("");
  const flags = report.flags.filter((f) => f.severity !== "info");
  html += `<h3 class="section-title">Before issuance</h3>` + (flags.length
    ? flags.map((f) => `<div class="flag-row"><span class="chip ${f.severity === "error" ? "bad" : "warn"}">${esc(f.severity)}</span><span>${esc(f.message)}</span></div>`).join("")
    : `<div class="flag-row"><span class="chip ok">clear</span><span>No red flags in the terms.</span></div>`);
  const notes = [...report.notes, ...report.flags.filter((f) => f.severity === "info").map((f) => f.message)];
  if (notes.length) html += `<h3 class="section-title">Assumptions</h3>` + notes.map((n) => `<div class="flag-row"><span>${esc(n)}</span></div>`).join("");
  $("#payments").innerHTML = html;
  $("#payments").querySelectorAll("[data-field]").forEach((b) => b.addEventListener("click", () => selectField(b.dataset.field)));
}

function share(part, total) { return `${Math.round((part / total) * 100)}%`; }

// ---------------------------------------------------------------- results

async function showResults() {
  $("#bond-view").classList.add("hidden");
  $("#sources").classList.add("hidden");
  $("#status").classList.add("hidden");
  $("#extract").classList.add("hidden");
  const view = $("#results-view");
  view.classList.remove("hidden");
  const results = await api("/api/results");
  const bonds = await api("/api/bonds");
  const truth = bonds.map((b) => b.reports.groundtruth).filter(Boolean);
  const payments = truth.reduce((a, s) => a + s.payments, 0);
  const allOk = truth.every((s) => s.payments_ok);
  const probes = truth.reduce((a, s) => a + Number(s.probes.split("/")[0]), 0);
  const probesTotal = truth.reduce((a, s) => a + Number(s.probes.split("/")[1]), 0);
  const mutants = results.contracts || [];
  const termRows = results.terms || [];
  const withEffect = termRows.reduce((a, r) => a + r.with_effect, 0);
  const caught = termRows.reduce((a, r) => a + r.caught, 0);
  let html = `<h1>Results</h1><p class="lede">Eighteen live fixed-rate bonds: IBRD notes including a digitally native one, the Hong Kong
    digital green bonds, and supranational, sovereign and corporate notes from public filings. Every number below can be reproduced with
    <code>tstt verify</code>, <code>tstt mutate</code> and <code>tstt eval</code>.</p>
    <div class="cards">
      <div class="card"><div class="big">${payments}</div><div class="what">payments on ${truth.length} bonds where the engine, QuantLib, the token on chain and the printed amounts ${allOk ? "all agree" : "do not all agree"}</div></div>
      <div class="card"><div class="big">${probes}/${probesTotal}</div><div class="what">guard probes rejected on chain (early payment, wrong payer, double payment, early redemption, unpaid coupon)</div></div>
      <div class="card"><div class="big">${mutants.filter((m) => m.caught).length}/${mutants.length}</div><div class="what">deliberate bugs in the bond contract caught by replaying the 18 bonds and the contract tests</div></div>
      <div class="card"><div class="big">${caught}/${withEffect}</div><div class="what">injected term errors that change a cash flow, flagged by the consistency checks before issuance</div></div>
    </div>`;
  if (results.eval.length) {
    html += `<h2>Reading the documents</h2><div class="table-wrap"><table><tr><th>Model</th><th class="num">Bonds</th><th class="num">Field accuracy</th><th class="num">Bonds with all cash flows right</th><th class="num">Quotes on cited page</th><th class="num">Time per bond</th><th class="num">Cost</th></tr>` +
      results.eval.map((e) => {
        const [mode, ...model] = e.run.split("-");
        return `<tr><td>${esc(modelName(model.join("-")))} · ${esc(mode.replace("_", "-"))}</td><td class="num">${e.submitted}/${bonds.length}</td><td class="num">${e.field_accuracy == null ? "–" : (e.field_accuracy * 100).toFixed(1) + "%"}</td><td class="num">${e.bonds_cash_flows_match}/${bonds.length}</td><td class="num">${esc(e.quotes_on_page)}</td><td class="num">${e.seconds_per_bond ?? "–"} s</td><td class="num">${e.cost_usd == null ? "local" : "$" + e.cost_usd.toFixed(2)}</td></tr>`;
      }).join("") + `</table></div>`;
  }
  if (mutants.length) {
    html += `<h2>Bugs planted in the contract</h2><div class="table-wrap"><table><tr><th>Mutant</th><th>What it breaks</th><th class="num">Bonds exposing it</th><th class="num">Failing tests</th><th>Result</th></tr>` +
      mutants.map((m) => `<tr><td><code>${esc(m.name)}</code></td><td>${esc(m.description)}</td><td class="num">${m.bonds_caught.length}</td><td class="num">${m.failed_tests.length}</td><td><span class="chip ${m.caught ? "ok" : "bad"}">${m.caught ? "caught" : "survived"}</span></td></tr>`).join("") + `</table></div>`;
  }
  if (termRows.length) {
    html += `<h2>Errors planted in the terms</h2><div class="table-wrap"><table><tr><th>Error</th><th class="num">Bonds</th><th class="num">Changes a cash flow</th><th class="num">Flagged</th><th class="num">by page check</th><th class="num">by consistency checks</th><th>Missed</th></tr>` +
      termRows.map((r) => `<tr><td>${esc(r.description)}</td><td class="num">${r.cases}</td><td class="num">${r.with_effect}</td><td class="num">${r.caught}</td><td class="num">${r.by_page}</td><td class="num">${r.by_checks}</td><td>${esc(r.missed.join(", ") || "–")}</td></tr>`).join("") + `</table></div>`;
  }
  view.innerHTML = html;
}

init();
