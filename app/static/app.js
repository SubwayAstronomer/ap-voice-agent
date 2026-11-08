/* AP Autopilot front end: hash router, live run view over SSE, dashboard,
   exception queue and reference data. Plain JS, no build step. */
"use strict";

var view = document.getElementById("view");
var state = { stream: null, lastRun: null, timer: null, health: null, open: {} };

/* ---------------------------------------------------------------- helpers */
function esc(s) {
  return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function inr(x) {
  if (x === null || x === undefined || x === "") return "-";
  var n = Number(x), neg = n < 0, s = Math.abs(n).toFixed(2).split("."), w = s[0];
  if (w.length > 3) {
    var head = w.slice(0, -3), tail = w.slice(-3), parts = [];
    while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2); }
    if (head) parts.unshift(head);
    w = parts.join(",") + "," + tail;
  }
  return (neg ? "-" : "") + "INR " + w + "." + s[1];
}
function num(x) { return x === null || x === undefined ? "-" : Number(x).toLocaleString("en-IN"); }
function ms(x) { return x ? (x / 1000).toFixed(1) + " s" : "-"; }
function time(s) { return s ? s.replace("T", " ").slice(0, 19) : "-"; }
function api(path, opts) {
  return fetch(path, opts).then(function (r) {
    if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.detail || r.statusText); });
    return r.json();
  });
}
function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}
function toast(msg) {
  var t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(function () { t.classList.remove("show"); }, 2600);
}
var ICON = {
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 12l5 5L20 7"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"><path d="M12 4.5v10M12 19.5v.2"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  skip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M6 12h12"/></svg>'
};
function chip(text, cls) { return '<span class="chip ' + esc(cls || text) + '">' + esc(text) + "</span>"; }
function decisionLabel(d) { return { APPROVE: "Approve", HOLD: "Hold", REJECT: "Reject" }[d] || d || "-"; }
var SEV = {};
var WORKING = { intake: "Hashing the file and checking for a text layer", extract: "Reading the fields",
  validate: "Checking arithmetic, GSTIN and tax type", vendor: "Looking up the vendor and earlier invoices",
  po: "Locating the purchase order", match: "Matching lines against the PO and goods receipts",
  decide: "Applying the decision rules" };

/* ---------------------------------------------------------------- router */
function route() {
  if (state.stream) { state.stream.close(); state.stream = null; }
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  var h = location.hash.replace(/^#\/?/, "") || "new";
  var parts = h.split("/");
  document.querySelectorAll("#nav a").forEach(function (a) { a.classList.toggle("active", a.dataset.r === parts[0]); });
  if (parts[0] === "run") {
    if (parts[1]) return renderRun(parseInt(parts[1], 10));
      return api("/api/runs").then(function (rs) {
        if (rs.length) {
          // Always show the invoice currently being processed first
          var active = rs.find(function (r) {
            return r.status === "running";
          });
      
          // If nothing is running, show the oldest queued invoice
          if (!active) {
            active = rs
              .filter(function (r) {
                return r.status === "queued";
              })
              .sort(function (a, b) {
                return a.run_id - b.run_id
              })[0];
          }
      
          // Otherwise show the latest completed run
          if (!active) active = rs[0];
      
          location.hash = "#/run/" + active.run_id;
        } else {
          view.innerHTML =
            '<div class="empty card">No runs yet. Start one from <a href="#/new">New run</a>.</div>';
        }
      });
;
  }
  if (parts[0] === "dashboard") return renderDashboard();
  if (parts[0] === "queue") return renderQueue();
  if (parts[0] === "reference") return renderReference();
  if (parts[0] === "rules") return renderRules();
  if (parts[0] === "quality") return renderQuality();
  return renderNew();
}
window.addEventListener("hashchange", route);

function refreshQueueCount() {
  api("/api/dashboard").then(function (d) {
    var c = document.getElementById("qcount");
    c.hidden = !d.open_holds; c.textContent = d.open_holds;
  }).catch(function () {});
}

/* ---------------------------------------------------------------- new run */
function renderNew() {
  view.innerHTML =
    '<div class="page-head"><div><h1>Process an invoice</h1><p>Upload a vendor invoice PDF, or run one of the prepared scenarios.</p></div>' +
    '<div class="actions"><button class="btn" id="resetBtn">Reset demo data</button>' +
    '<button class="btn primary" id="allBtn">Run all 10 in order</button></div></div>' +
    '<input type="file" accept="application/pdf" id="file" style="display:none">' +
    '<div class="drop card" id="drop" role="button" tabindex="0"><div class="drop-title">Drop an invoice PDF here</div>' +
    '<div class="muted">or click to choose a file. Text PDFs and scans are both fine.</div></div>' +
    '<div class="section-title">Prepared scenarios</div><div class="samples" id="samples"></div>';
  var drop = document.getElementById("drop"), file = document.getElementById("file");
  ["dragover", "dragenter"].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); }); });
  ["dragleave", "drop"].forEach(function (ev) { drop.addEventListener(ev, function () { drop.classList.remove("over"); }); });
  drop.addEventListener("drop", function (e) { e.preventDefault(); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });
  drop.addEventListener("click", function () { file.click(); });
  drop.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
  file.addEventListener("change", function () { if (file.files[0]) upload(file.files[0]); file.value = ""; });
  document.getElementById("allBtn").onclick = function () {
    post("/api/runs/samples/all").then(function (r) { toast("Queued " + r.run_ids.length + " invoices"); location.hash = "#/run/" + r.run_ids[0]; });
  };
  document.getElementById("resetBtn").onclick = function () {
    if (!confirm("Clear all runs and billed-to-date? Reference data stays.")) return;
    post("/api/reset").then(function () { toast("Demo data reset"); refreshQueueCount(); });
  };
  api("/api/samples").then(function (list) {
    document.getElementById("samples").innerHTML = list.map(function (s) {
      return '<div class="card sample"><div class="top"><span class="name">' + esc(s.file) + "</span>" +
        chip(s.format === "scan" ? "Scan" : "Text PDF", "neutral") + "</div><p>" + esc(s.scenario) + "</p>" +
        '<div class="row"><span class="small muted">Expected ' + chip(decisionLabel(s.decision), s.decision) + "</span>" +
        '<span><a class="btn small" target="_blank" href="/api/samples/' + encodeURIComponent(s.file) + '">PDF</a> ' +
        '<button class="btn small primary" data-file="' + esc(s.file) + '">Run</button></span></div></div>';
    }).join("");
    document.querySelectorAll("[data-file]").forEach(function (b) {
      b.onclick = function () { post("/api/runs/sample", { file: b.dataset.file }).then(function (r) { location.hash = "#/run/" + r.run_id; }); };
    });
  });
}
function upload(f) {
  var fd = new FormData(); fd.append("file", f);
  api("/api/runs", { method: "POST", body: fd }).then(function (r) { location.hash = "#/run/" + r.run_id; })
    .catch(function (e) { toast(e.message); });
}

/* ---------------------------------------------------------------- run view */
function renderRun(id) {
  state.open = {};
  view.innerHTML = '<div class="empty">Loading run #' + id + "...</div>";
  api("/api/runs/" + id).then(function (run) {
    view.innerHTML =
      '<div class="run-head"><h1 id="rtitle"></h1><span id="rstatus"></span><span class="muted small" id="rmeta"></span>' +
      '<span style="margin-left:auto" class="actions"><button class="btn small" id="rerunBtn" hidden title="Process this invoice again with the rules now in force">Re-run with current rules</button>' +
      '<a class="btn small" href="#/dashboard">All runs</a></span></div>' +
      '<div id="banner"></div><div class="runlayout"><div class="card pdfwrap"><div class="pdfbar"><b>Invoice</b><a class="btn small" target="_blank" href="/api/runs/' + id + '/file">Open PDF</a></div>' +
      '<div class="pdfscroll"><img alt="Invoice page 1" src="/api/runs/' + id + '/preview.png"></div></div>' +
      '<div><div class="steps" id="steps"></div><div id="after"></div></div></div>';
    paintRun(run);
    if (run.status !== "done" && run.status !== "error" && run.status !== "superseded") listen(id);
    document.getElementById("rerunBtn").onclick = function () {
      post("/api/runs/" + id + "/rerun").then(function (r) { toast("Re-running with current rules"); location.hash = "#/run/" + r.run_id; })
        .catch(function (e) { toast(e.message); });
    };
  }).catch(function (e) { view.innerHTML = '<div class="empty card">' + esc(e.message) + "</div>"; });
}
function listen(id) {
  if (!window.EventSource) return poll(id);
  var es = new EventSource("/api/runs/" + id + "/stream");
  state.stream = es;
  es.onmessage = function (m) { try { paintRun(JSON.parse(m.data)); } catch (e) { /* ignore partial */ } };
  es.addEventListener("end", function () { es.close(); state.stream = null; refreshQueueCount(); });
  es.onerror = function () { es.close(); state.stream = null; poll(id); };
}
function poll(id) {
  state.timer = setInterval(function () {
    api("/api/runs/" + id).then(function (r) { paintRun(r); if (r.status === "done" || r.status === "error") { clearInterval(state.timer); state.timer = null; } });
  }, 700);
}

function paintRun(run) {
  state.lastRun = run;
  var fin = run.status === "done" || run.status === "superseded";
  var rb = document.getElementById("rerunBtn"); if (rb) rb.hidden = !(fin || run.status === "error");
  document.getElementById("rtitle").textContent = "Run #" + run.run_id + "  " + (run.invoice ? run.invoice.file_name : "");
  document.getElementById("rstatus").innerHTML = chip(run.status, fin ? "neutral" : run.status);
  document.getElementById("rmeta").textContent = (run.mode ? run.mode.split(":").slice(0, 2).join(" / ") + "  |  " : "") +
    "started " + time(run.started_at) + (run.duration_ms ? "  |  " + ms(run.duration_ms) : "");

  var b = document.getElementById("banner");
  if (fin) {
    var fd = run.final_decision, ex = run.explanation || {};
    var seen = {}, codes = (run.flags || []).filter(function (f) {
      if (f.severity === "INFO" || seen[f.code]) return false; seen[f.code] = 1; return true; });
    var ov = run.override ? '<span class="chip neutral">Overridden by ' + esc(run.override.by) + ": was " + esc(run.override.previous) + "</span>" : "";
    b.innerHTML = '<div class="banner ' + fd + '"><div class="big">' + decisionLabel(fd).toUpperCase() + "</div>" +
      '<div class="why">' + esc(ex.summary || ex.facts || "") + "</div>" +
      '<div class="meta">' + (run.owner && fd === "HOLD" ? chip("Owner: " + run.owner, "neutral") : "") +
      codes.map(function (f) { return '<span class="chip code sev-' + f.severity + '">' + esc(f.code) + "</span>"; }).join("") + ov +
      (run.status === "superseded" ? '<span class="chip neutral">Superseded by a later re-run</span>' : "") + "</div></div>";
  } else if (run.status === "error") {
    b.innerHTML = '<div class="banner REJECT"><div class="big">ERROR</div><div class="why">The run failed. See the stage marked in red. ' +
      "Nothing was posted or sent.</div></div>";
  } else {
    var cur = (run.events || []).filter(function (e) { return e.status === "running"; })[0];
    b.innerHTML = '<div class="banner running"><div class="big">RUNNING</div><div class="why">' +
      (cur ? "Stage " + cur.stage_no + " of 7: " + esc(cur.name) : "Queued") + "</div></div>";
  }

  var steps = document.getElementById("steps");
  steps.innerHTML = (run.events || []).map(function (e) {
    var st = e.status, cls = st, ic;
    if (st === "running") ic = '<div class="spin"></div>';
    else if (st === "pass" || st === "approve") ic = ICON.check;
    else if (st === "flag" || st === "hold") ic = ICON.alert;
    else if (st === "error" || st === "reject") ic = ICON.x;
    else if (st === "skipped") ic = ICON.skip;
    else ic = String(e.stage_no);
    var isOpen = state.open[e.stage] !== undefined ? state.open[e.stage] : (st === "flag" || e.stage === "decide" && fin);
    var label = { pass: "Pass", flag: "Flagged", running: "Running", waiting: "Waiting", skipped: "Skipped", error: "Error",
      approve: "Approve", hold: "Hold", reject: "Reject" }[st] || st;
    var chipCls = { approve: "APPROVE", hold: "HOLD", reject: "REJECT" }[st] || st;
    return '<div class="step ' + cls + (isOpen && e.detail ? " open" : "") + '" data-stage="' + e.stage + '">' +
      '<div class="hd"><div class="ic">' + ic + '</div><div><div class="ttl">' + e.stage_no + ". " + esc(e.name) + "</div>" +
      '<div class="sum">' + esc(st === "waiting" ? "" : st === "running" ? (WORKING[e.stage] || "Working") + "..." : e.summary) + "</div></div>" +
      '<div style="text-align:right">' + chip(label, chipCls) + '<div class="dur">' + (e.duration_ms ? ms(e.duration_ms) :
        st === "running" ? '<span class="elapsed" data-k="' + run.run_id + "-" + e.stage + '"></span>' : "") + "</div></div></div>" +
      '<div class="bd">' + (e.detail ? stageDetail(e, run) : "") + "</div></div>";
  }).join("");
  steps.querySelectorAll(".step .hd").forEach(function (hd) {
    hd.onclick = function () {
      var s = hd.parentNode; s.classList.toggle("open"); state.open[s.dataset.stage] = s.classList.contains("open");
    };
  });
  tickElapsed();
  document.getElementById("after").innerHTML = fin ? afterPanel(run) : "";
  wireOverride(run);
}

function flagCallouts(flags) {
  return (flags || []).map(function (f) {
    return '<div class="callout ' + f.severity + '">' + chip(f.severity === "INFO" ? "Note" : f.severity, f.severity) +
      "<div><b>" + esc(f.code) + "</b>" + (f.owner ? ' <span class="muted">(' + esc(f.owner) + ")</span>" : "") + "<br>" + esc(f.message) + "</div></div>";
  }).join("");
}
function checksList(checks) {
  return (checks || []).map(function (c) {
    return '<div class="check"><div class="dot ' + c.result + '">' + (c.result === "pass" ? ICON.check : ICON.alert) + "</div>" +
      "<div><b>" + esc(c.check) + '</b><div class="small muted">' + esc(c.detail) + "</div></div></div>";
  }).join("");
}
function kv(obj) {
  return '<dl class="kv">' + Object.keys(obj).map(function (k) {
    return "<dt>" + esc(k.replace(/_/g, " ")) + "</dt><dd>" + esc(obj[k]) + "</dd>";
  }).join("") + "</dl>";
}

function stageDetail(e, run) {
  var d = e.detail || {}, h = flagCallouts(d.stage_flags);
  if (e.stage === "intake") {
    h += kv({ file: d.file, format: d.format, pages: d.pages, characters_read: d.characters_read, document_type: d.document_type, sha256: d.sha256 });
  } else if (e.stage === "extract") {
    var thr = d.threshold || 0.85, f = d.fields || {};
    var order = ["vendor_name", "supplier_gstin", "buyer_gstin", "invoice_no", "invoice_date", "po_ref", "subtotal", "cgst", "sgst", "igst",
      "total", "prices_include_tax", "bank_account", "bank_ifsc"];
    h += '<div class="small muted" style="margin-bottom:6px">Read by ' + esc(d.mode) + ". Confidence below " + thr + " on a key field puts the invoice on hold.</div>";
    h += '<div class="tbl"><table><tr><th>Field</th><th>Value</th><th>Confidence</th><th>Read from</th></tr>' +
      order.filter(function (k) { return f[k]; }).map(function (k) {
        var x = f[k], c = Number(x.confidence || 0), v = x.value;
        if (["subtotal", "cgst", "sgst", "igst", "total"].indexOf(k) >= 0) v = inr(v);
        if (k === "prices_include_tax") v = v ? "yes" : "no";
        return "<tr><td>" + esc(k.replace(/_/g, " ")) + '</td><td class="mono">' + esc(v) + (x.verified ? ' <span class="chip pass">' + esc(x.verified) + "</span>" : "") +
          '</td><td><span class="bar' + (c < thr ? " low" : "") + '"><i style="width:' + Math.round(c * 100) + '%"></i></span>' + c.toFixed(2) +
          '</td><td><div class="src" title="' + esc(x.source) + '">' + esc(x.source) + "</div></td></tr>";
      }).join("") + "</table></div>";
    h += '<div class="section-title" style="margin-top:14px">Line items' + (d.lines_from ? ' (taken from ' + esc(d.lines_from) + ')' : '') + '</div><div class="tbl"><table><tr><th>#</th><th>Description</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th><th class="num">Conf.</th></tr>' +
      (d.lines || []).map(function (l, i) {
        return "<tr><td>" + (i + 1) + "</td><td>" + esc(l.description) + '</td><td class="num">' + num(l.qty) + " " + esc(l.uom) +
          '</td><td class="num">' + inr(l.unit_price) + '</td><td class="num">' + inr(l.amount) + '</td><td class="num">' + Number(l.confidence).toFixed(2) + "</td></tr>";
      }).join("") + "</table></div>";
  } else if (e.stage === "validate") {
    h += checksList(d.checks);
  } else if (e.stage === "vendor") {
    if (d.vendor) h += kv({ vendor: d.vendor.legal_name, vendor_id: d.vendor.vendor_id, gstin: d.vendor.gstin, normalised_invoice_no: d.normalised_invoice_no });
    h += '<div style="margin-top:8px">' + checksList(d.checks) + "</div>";
  } else if (e.stage === "po") {
    h += '<div class="small muted" style="margin-bottom:6px">Method: ' + (d.method === "explicit" ? "PO number quoted on the invoice" :
      d.method === "inferred" ? "no PO on the invoice; scored the vendor's open POs (70% line descriptions, 30% amount)" : "none") + "</div>";
    if (d.candidates && d.candidates.length) {
      h += '<div class="tbl"><table><tr><th>PO</th><th>Date</th><th class="num">Open value</th><th class="num">Lines</th><th class="num">Amount</th><th class="num">Score</th></tr>' +
        d.candidates.map(function (c, i) {
          return "<tr" + (i === 0 ? ' class="flagrow"' : "") + "><td><b>" + esc(c.po_number) + "</b></td><td>" + esc(c.po_date) + '</td><td class="num">' + inr(c.open_value) +
            '</td><td class="num">' + c.description_score + '</td><td class="num">' + c.amount_score + '</td><td class="num"><b>' + c.score + "</b></td></tr>";
        }).join("") + "</table></div>";
    }
    if (d.po) {
      h += '<div class="section-title" style="margin-top:14px">' + esc(d.po.po_number) + " (dated " + esc(d.po.po_date) + ")</div>" +
        '<div class="tbl"><table><tr><th>#</th><th>Item</th><th class="num">Qty</th><th class="num">Unit price</th></tr>' +
        d.po.lines.map(function (l) {
          return "<tr><td>" + l.line_no + "</td><td>" + esc(l.description) + '</td><td class="num">' + num(l.qty) + " " + esc(l.uom) + '</td><td class="num">' + inr(l.unit_price) + "</td></tr>";
        }).join("") + "</table></div>";
    }
  } else if (e.stage === "match") {
    if (d.skipped) h += '<div class="muted">No PO was identified, so there is nothing to match against.</div>';
    else h += '<div class="tbl"><table class="match"><tr><th>#</th><th>Invoice line &rarr; PO line</th><th class="num">Quantity</th>' +
      '<th class="num">Unit price (INR)</th><th></th></tr>' +
      (d.rows || []).map(function (r) {
        var money2 = function (x) { return x === undefined || x === null ? "-" : Number(x).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
        var v = r.variance_pct === undefined ? "" : " (" + (r.variance_pct > 0 ? "+" : "") + r.variance_pct.toFixed(1) + "%)";
        return '<tr class="' + (r.status === "flag" ? "flagrow" : "") + '"><td>' + r.line + "</td><td>" + esc(r.description) +
          '<div class="small muted">' + (r.po_line ? "PO line " + r.po_line + " (match " + r.match_score + ")" : "no PO line") + "</div>" +
          '</td><td class="num"><b>' + num(r.qty) + "</b> on this invoice" + (r.po_line ? '<div class="small muted">' + num(r.billed_before) +
          " billed before &middot; " + num(r.grn_qty) + " received &middot; " + num(r.po_qty) + " ordered</div>" : "") +
          '</td><td class="num"><b>' + money2(r.price_ex_tax) + "</b>" + (r.po_line ? '<div class="small muted">PO ' + money2(r.po_price) + v + "</div>" : "") +
          "</td><td>" + (r.status === "pass" ? chip("Match", "pass") : chip((r.issues || []).join(", ") || "Flag", "flag")) + "</td></tr>";
      }).join("") + "</table></div>";
  } else if (e.stage === "decide") {
    h = '<div class="small muted" style="margin-bottom:8px">Rule: ' + esc(d.rule) + "</div>";
    var all = d.flags || [];
    h += all.length ? flagCallouts(all) : '<div class="callout INFO">No flags raised by any stage.</div>';
  }
  return h;
}

function afterPanel(run) {
  var na = run.next_action || {}, h = '<div class="card pad" style="margin-top:12px"><h3>Next action: ' + esc(na.title || "") + "</h3>";
  if (na.type === "post_to_erp") {
    var p = Object.assign({}, na.payload || {});
    p.gross_amount = inr(p.gross_amount); p.tax_amount = inr(p.tax_amount);
    h += kv(p);
  } else if (na.type === "exception_queue") {
    h += '<ul style="margin:0;padding-left:18px">' + (na.questions || []).map(function (q) {
      return "<li><b>" + esc(q.owner) + ":</b> " + esc(q.question) + "</li>";
    }).join("") + "</ul>";
  } else if (na.type === "vendor_email") {
    h += '<div class="small"><b>Subject:</b> ' + esc(na.subject) + '</div><pre class="email" id="email">' + esc(na.body) + "</pre>" +
      '<div style="margin-top:8px"><button class="btn small" id="copyEmail">Copy email</button></div>';
  }
  h += "</div>";
  if (run.final_decision === "HOLD" && !run.override && run.status === "done") {
    h += '<div class="card pad" style="margin-top:12px"><h3>Resolve this hold</h3><div class="small muted" style="margin-bottom:8px">' +
      "Every override is logged with a reason and shows on the dashboard.</div>" +
      '<textarea id="ovc" placeholder="Reason (required), e.g. confirmed new bank account by phone with vendor" style="width:100%;min-height:60px;border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit"></textarea>' +
      '<div class="actions" style="margin-top:8px"><button class="btn ok" data-ov="APPROVE">Approve</button><button class="btn bad" data-ov="REJECT">Reject</button></div></div>';
  }
  if (run.override) {
    h += '<div class="card pad" style="margin-top:12px"><h3>Override</h3>' + kv({ decision: run.override.action, previous: run.override.previous,
      by: run.override.by, at: time(run.override.at), reason: run.override.comment }) + "</div>";
  }
  return h;
}
function wireOverride(run) {
  var c = document.getElementById("copyEmail");
  if (c) c.onclick = function () { navigator.clipboard.writeText(document.getElementById("email").textContent).then(function () { toast("Email copied"); }); };
  document.querySelectorAll("[data-ov]").forEach(function (b) {
    b.onclick = function () {
      var txt = document.getElementById("ovc").value;
      post("/api/runs/" + run.run_id + "/override", { action: b.dataset.ov, comment: txt, by: "AP clerk" })
        .then(function (r) { paintRun(r); refreshQueueCount(); toast("Decision recorded"); })
        .catch(function (e) { toast(e.message); });
    };
  });
}

/* ---------------------------------------------------------------- dashboard */
function renderDashboard() {
  view.innerHTML = '<div class="page-head"><div><h1>Dashboard</h1><p>History, status and outputs across every run.</p></div>' +
    '<div class="actions"><a class="btn primary" href="#/new">New run</a></div></div><div id="dash"></div>';
  var paint = function () {
    Promise.all([api("/api/dashboard"), api("/api/runs")]).then(function (res) {
      var d = res[0], runs = res[1], by = d.by_decision, n = d.processed || 0;
      var pct = function (x) { return n ? (100 * x / n) : 0; };
      var maxR = Math.max.apply(null, [1].concat(d.reasons.map(function (r) { return r[1]; })));
      var html = '<div class="kpis">' +
        kpi("Invoices processed", n, inr(d.value) + " in total") +
        kpi("Straight-through", d.straight_through_rate + "%", d.straight_through + " approved with no human touch") +
        kpi("Open holds", d.open_holds, '<a href="#/queue">Go to exception queue</a>') +
        kpi("Avg time per invoice", ms(d.avg_ms), "includes demo pacing") +
        kpi("Human overrides", d.overrides, "each with a logged reason") + "</div>";
      html += '<div class="grid2"><div class="card pad"><h3>Final decisions</h3><div class="stack">' +
        '<i style="width:' + pct(by.APPROVE) + '%;background:var(--ok)"></i><i style="width:' + pct(by.HOLD) + '%;background:#d97706"></i>' +
        '<i style="width:' + pct(by.REJECT) + '%;background:var(--bad)"></i></div><div class="legend">' +
        '<span style="--c:var(--ok)">Approve ' + by.APPROVE + '</span><span style="--c:#d97706">Hold ' + by.HOLD + '</span><span style="--c:var(--bad)">Reject ' + by.REJECT + "</span></div></div>" +
        '<div class="card pad"><h3>Why invoices were stopped</h3>' + (d.reasons.length ? d.reasons.map(function (r) {
          var sev = (r[0] === "DUPLICATE" || r[0] === "NOT_INVOICE" || r[0] === "MISSING_FIELD" || r[0] === "WRONG_BUYER") ? "REJECT" : "HOLD";
          return '<div class="hbar"><span class="mono">' + esc(r[0]) + '</span><span class="track"><i class="' + sev + '" style="width:' + (100 * r[1] / maxR) + '%"></i></span><span>' + r[1] + "</span></div>";
        }).join("") : '<div class="muted">Nothing stopped yet.</div>') + "</div></div>";
      html += '<div class="section-title">All runs</div><div class="card tbl"><table><tr><th>Run</th><th>Started</th><th>File</th><th>Vendor</th><th>Invoice no.</th>' +
        '<th class="num">Amount</th><th>Decision</th><th>Reasons</th><th class="num">Time</th></tr>' +
        (runs.length ? runs.map(function (r) {
          var codes = (r.reason_codes || []).filter(function (c) { return ["PO_INFERRED", "PRICE_WITHIN_TOLERANCE", "OCR_REPAIRED", "LLM_FALLBACK", "VENDOR_MATCHED_BY_NAME"].indexOf(c) < 0; });
          var dec = r.status === "done" ? chip(decisionLabel(r.final_decision), r.final_decision) + (r.override ? ' <span class="small muted">override</span>' : "") :
            r.status === "superseded" ? '<span style="opacity:.55">' + chip(decisionLabel(r.final_decision), r.final_decision) + '</span> <span class="small muted">superseded</span>' : chip(r.status, r.status);
          return '<tr class="click" data-run="' + r.run_id + '"><td>#' + r.run_id + "</td><td>" + time(r.started_at).slice(5, 16) + '</td><td class="mono">' + esc(r.file_name) +
            "</td><td>" + esc(r.vendor_name || "-") + '</td><td class="mono">' + esc(r.invoice_no || "-") + '</td><td class="num">' + inr(r.total) + "</td><td>" + dec +
            "</td><td>" + codes.map(function (c) { return '<span class="chip code">' + esc(c) + "</span>"; }).join(" ") + '</td><td class="num">' + ms(r.duration_ms) + "</td></tr>";
        }).join("") : '<tr><td colspan="9" class="empty">No runs yet.</td></tr>') + "</table></div>";
      document.getElementById("dash").innerHTML = html;
      document.querySelectorAll("[data-run]").forEach(function (tr) { tr.onclick = function () { location.hash = "#/run/" + tr.dataset.run; }; });
    });
  };
  paint();
  state.timer = setInterval(paint, 3000);
}
function kpi(lbl, val, sub) {
  return '<div class="card kpi"><div class="lbl">' + esc(lbl) + '</div><div class="val">' + esc(val) + '</div><div class="sub">' + sub + "</div></div>";
}

/* ---------------------------------------------------------------- queue */
function renderQueue() {
  view.innerHTML = '<div class="page-head"><div><h1>Exception queue</h1><p>Invoices on hold, grouped by who needs to act. Approve or reject with a reason; every override is logged.</p></div></div><div id="q"></div>';
  api("/api/runs").then(function (runs) {
    var holds = runs.filter(function (r) { return r.status === "done" && r.final_decision === "HOLD"; });
    if (!holds.length) { document.getElementById("q").innerHTML = '<div class="card empty">No open holds. Every processed invoice has a final decision.</div>'; return; }
    return Promise.all(holds.map(function (r) { return api("/api/runs/" + r.run_id); })).then(function (full) {
      var groups = {};
      full.forEach(function (r) { (groups[r.owner || "AP clerk"] = groups[r.owner || "AP clerk"] || []).push(r); });
      document.getElementById("q").innerHTML = Object.keys(groups).map(function (owner) {
        return '<div class="qgroup"><div class="section-title">' + esc(owner) + " (" + groups[owner].length + ")</div>" +
          groups[owner].map(function (r) {
            var qs = ((r.next_action || {}).questions || []);
            return '<div class="card qitem"><div><b>' + esc(r.vendor_name || "-") + '</b> <span class="mono muted">' + esc(r.invoice_no || "") + "</span> " +
              '<span class="muted">' + inr(r.total) + "</span><div class=\"small\" style=\"margin-top:4px\">" +
              (r.reason_codes || []).filter(function (c) { return c !== "PO_INFERRED" && c !== "PRICE_WITHIN_TOLERANCE"; }).map(function (c) { return '<span class="chip code sev-HOLD">' + esc(c) + "</span>"; }).join(" ") +
              '</div></div><div class="actions"><a class="btn small" href="#/run/' + r.run_id + '">Open run #' + r.run_id + "</a></div>" +
              '<ul class="qs">' + qs.map(function (q) { return "<li>" + esc(q.question) + "</li>"; }).join("") + "</ul>" +
              '<textarea data-c="' + r.run_id + '" placeholder="Reason for your decision (required)"></textarea>' +
              '<div class="actions" style="grid-column:1/-1"><button class="btn ok small" data-q="' + r.run_id + '" data-a="APPROVE">Approve</button>' +
              '<button class="btn bad small" data-q="' + r.run_id + '" data-a="REJECT">Reject</button></div></div>';
          }).join("") + "</div>";
      }).join("");
      document.querySelectorAll("[data-q]").forEach(function (b) {
        b.onclick = function () {
          var c = document.querySelector('[data-c="' + b.dataset.q + '"]').value;
          post("/api/runs/" + b.dataset.q + "/override", { action: b.dataset.a, comment: c, by: "AP clerk" })
            .then(function () { toast("Run #" + b.dataset.q + ": " + decisionLabel(b.dataset.a)); refreshQueueCount(); renderQueue(); })
            .catch(function (e) { toast(e.message); });
        };
      });
    });
  });
}

/* ---------------------------------------------------------------- reference */
function renderReference() {
  view.innerHTML = '<div class="page-head"><div><h1>Reference data</h1><p>The ERP stand-in the process checks against: vendor master, purchase orders, goods receipts and the rules in force.</p></div></div><div id="ref"></div>';
  api("/api/reference").then(function (d) {
    var h = '<div class="section-title">Vendor master</div><div class="card tbl"><table><tr><th>ID</th><th>Vendor</th><th>GSTIN</th><th>State</th><th>Bank a/c</th><th>IFSC</th></tr>' +
      d.vendors.map(function (v) {
        return "<tr><td>" + esc(v.vendor_id) + "</td><td>" + esc(v.legal_name) + '</td><td class="mono">' + esc(v.gstin) + "</td><td>" + esc(v.state_code) +
          '</td><td class="mono">xxxx' + esc(v.bank_account.slice(-4)) + '</td><td class="mono">' + esc(v.bank_ifsc) + "</td></tr>";
      }).join("") + "</table></div>";
    h += '<div class="section-title">Purchase orders: ordered, received and billed</div><div class="card tbl"><table><tr><th>PO</th><th>Vendor</th><th>Date</th><th>#</th><th>Item</th>' +
      '<th class="num">Unit price</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed</th></tr>' +
      d.purchase_orders.map(function (p) {
        return p.lines.map(function (l, i) {
          var over = l.billed_qty > l.grn_qty;
          return "<tr" + (over ? ' class="flagrow"' : "") + ">" + (i === 0 ? '<td rowspan="' + p.lines.length + '"><b>' + esc(p.po_number) + '</b></td><td rowspan="' + p.lines.length + '">' + esc(p.vendor_id) +
            '</td><td rowspan="' + p.lines.length + '">' + esc(p.po_date) + "</td>" : "") + "<td>" + l.line_no + "</td><td>" + esc(l.description) + '</td><td class="num">' + inr(l.unit_price) +
            '</td><td class="num">' + num(l.qty) + '</td><td class="num">' + num(l.grn_qty) + '</td><td class="num">' + num(l.billed_qty) + "</td></tr>";
        }).join("");
      }).join("") + "</table></div>";
    var c = d.config;
    h += '<div class="section-title">Rules in force (config.yaml)</div><div class="card pad">' + kv({
      buyer: d.buyer.name + " (GSTIN " + d.buyer.gstin + ")",
      min_extraction_confidence: c.extraction.min_confidence,
      total_tolerance: inr(c.validation.total_tolerance_inr),
      price_tolerance: c.matching.price_tolerance_pct + "% over PO",
      near_duplicate: "same vendor, total within " + c.duplicates.near_total_pct + "%, within " + c.duplicates.near_days + " days",
      po_inference: "best score at least " + c.po_inference.min_score + ", ahead of runner-up by " + c.po_inference.min_margin,
      auto_approve_limit: inr(c.approval.auto_approve_limit_inr)
    }) + "</div>";
    document.getElementById("ref").innerHTML = h;
  });
}

/* ---------------------------------------------------------------- elapsed timer */
var seenRunning = {};
function tickElapsed() {
  document.querySelectorAll(".elapsed").forEach(function (el) {
    var k = el.dataset.k;
    if (!seenRunning[k]) seenRunning[k] = Date.now();
    el.textContent = Math.round((Date.now() - seenRunning[k]) / 1000) + " s";
  });
}
setInterval(tickElapsed, 500);

/* ---------------------------------------------------------------- accuracy */
function renderQuality() {
  view.innerHTML = '<div class="page-head"><div><h1>Accuracy</h1><p>The latest run of each sample invoice, scored against the ground truth ' +
    'written by the script that generated the PDFs: the decision and its key reasons, plus 8 extracted fields.</p></div>' +
    '<div class="actions"><button class="btn primary" id="qAll">Run all 10 in order</button></div></div><div id="qual"><div class="empty">Loading...</div></div>';
  document.getElementById("qAll").onclick = function () {
    post("/api/runs/samples/all").then(function () { toast("Queued 10 invoices; this page refreshes as they finish"); });
  };
  var labels = { invoice_no: "Invoice no.", invoice_date: "Date", supplier_gstin: "GSTIN", po_ref: "PO ref", total: "Total",
    tax: "Tax", line_count: "Lines", bank_account: "Bank a/c" };
  var paint = function () {
    api("/api/evaluation").then(function (d) {
      if (!d.scored) {
        document.getElementById("qual").innerHTML = '<div class="card empty">No sample invoices processed yet. Click <b>Run all 10 in order</b>.</div>';
        return;
      }
      var cell = function (f) {
        var tip = "read: " + (f.got === null || f.got === undefined ? "(none)" : f.got) + " | truth: " + (f.want === null || f.want === undefined ? "(none)" : f.want);
        return '<td class="num" title="' + esc(tip) + '"><span class="dot ' + (f.ok ? "pass" : "flag") + '" style="display:inline-grid">' + (f.ok ? ICON.check : ICON.x) + "</span></td>";
      };
      var h = (d.rules_changed && d.rules_changed.length ? '<div class="callout HOLD" style="margin-bottom:12px"><div>Expected outcomes assume the default rules. ' +
        'Changed now: <b>' + esc(d.rules_changed.join(", ")) + '</b>. Runs made under changed rules can differ on purpose.</div></div>' : "") +
        '<div class="kpis" style="grid-template-columns:repeat(4,1fr)">' +
        kpi("Correct decisions", d.decisions_ok + " / " + d.scored, "decision and key reason codes match") +
        kpi("Field accuracy", d.field_accuracy + "%", d.scored * d.fields.length + " fields checked") +
        kpi("Scans", d.rows.filter(function (r) { return r.scored && r.format === "scan"; }).length, "of " + d.scored + " scored invoices") +
        kpi("Samples scored", d.scored + " / " + d.total, d.scored < d.total ? "run the rest to complete" : "all samples covered") + "</div>";
      h += '<div class="card tbl"><table><tr><th>Sample</th><th>Format</th><th>Read by</th><th>Expected</th><th>Got</th><th class="num">Decision</th>' +
        d.fields.map(function (k) { return '<th class="num">' + esc(labels[k] || k) + "</th>"; }).join("") + "</tr>" +
        d.rows.map(function (r) {
          if (!r.scored) return '<tr><td class="mono">' + esc(r.file) + '</td><td>' + esc(r.format) + '</td><td colspan="' + (4 + d.fields.length) + '" class="muted">not run yet</td></tr>';
          return '<tr class="click" data-run="' + r.run_id + '"><td class="mono">' + esc(r.file) + "</td><td>" + esc(r.format) + '</td><td class="small">' +
            esc((r.mode || "").split(":")[0].toUpperCase()) + "</td><td>" + chip(decisionLabel(r.expected), r.expected) + "</td><td>" + chip(decisionLabel(r.got), r.got) +
            "</td>" + cell({ ok: r.decision_ok, got: r.got, want: r.expected }) + d.fields.map(function (k) { return cell(r.fields[k]); }).join("") + "</tr>";
        }).join("") + '<tr><td colspan="6"><b>Per-field accuracy</b></td>' + d.fields.map(function (k) {
          return '<td class="num"><b>' + (d.per_field[k] === null ? "-" : Math.round(d.per_field[k]) + "%") + "</b></td>"; }).join("") + "</tr></table></div>" +
        '<p class="small muted">Hover a cell to see what was read against the truth. Click a row to open the run.</p>';
      document.getElementById("qual").innerHTML = h;
      document.querySelectorAll("#qual [data-run]").forEach(function (tr) { tr.onclick = function () { location.hash = "#/run/" + tr.dataset.run; }; });
    });
  };
  paint();
  state.timer = setInterval(paint, 4000);
}

/* ---------------------------------------------------------------- rules */
function renderRules() {
  view.innerHTML = '<div class="page-head"><div><h1>Rules</h1><p>The thresholds the decision engine uses. Changes apply to the next run; ' +
    'use <b>Re-run with current rules</b> on any run to see the effect.</p></div>' +
    '<div class="actions"><button class="btn" id="rulesReset">Restore defaults</button><button class="btn primary" id="rulesSave">Save rules</button></div></div>' +
    '<div class="card" id="rulesBox"><div class="empty">Loading...</div></div>';
  var paint = function (list) {
    document.getElementById("rulesBox").innerHTML = '<div class="tbl"><table><tr><th style="width:30%">Rule</th><th style="width:210px">Value</th><th>What it does</th><th class="num">Default</th></tr>' +
      list.map(function (r) {
        var changed = Number(r.value) !== Number(r.default);
        return "<tr" + (changed ? ' class="flagrow"' : "") + "><td><b>" + esc(r.label) + "</b>" + (changed ? ' <span class="chip flag">changed</span>' : "") +
          '</td><td><input class="ruleinp" type="number" data-key="' + esc(r.key) + '" value="' + esc(r.value) + '" min="' + r.min + '" max="' + r.max + '" step="' + r.step + '"> ' +
          '<span class="small muted unit">' + esc(r.unit) + '</span></td><td class="small">' + esc(r.help) + '</td><td class="num muted">' + esc(r.default) + "</td></tr>";
      }).join("") + "</table></div>";
  };
  api("/api/rules").then(paint);
  document.getElementById("rulesSave").onclick = function () {
    var changes = {};
    document.querySelectorAll(".ruleinp").forEach(function (i) { changes[i.dataset.key] = i.value; });
    api("/api/rules", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) })
      .then(function (l) { paint(l); toast("Rules saved. They apply to the next run."); }).catch(function (e) { toast(e.message); });
  };
  document.getElementById("rulesReset").onclick = function () {
    post("/api/rules/reset").then(function (l) { paint(l); toast("Defaults restored"); });
  };
}

/* ---------------------------------------------------------------- boot */
api("/api/health").then(function (h) {
  state.health = h; document.getElementById("mode").textContent = h.extraction;
  document.getElementById("llmTest").hidden = !h.llm;
}).catch(function () {});
document.getElementById("llmTest").onclick = function () {
  var out = document.getElementById("llmResult");
  out.textContent = "Testing..."; out.className = "llm-result";
  api("/api/llm-check").then(function (r) {
    out.textContent = r.message; out.className = "llm-result " + (r.ok ? "ok" : "bad");
  }).catch(function (e) { out.textContent = e.message; out.className = "llm-result bad"; });
};
refreshQueueCount();
route();
