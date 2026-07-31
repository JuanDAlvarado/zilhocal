(function () {
  "use strict";

  const form = document.getElementById("calc-form");
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const preview = document.getElementById("preview");
  const ocrStatus = document.getElementById("ocr-status");
  const resultsSection = document.getElementById("results");
  const resultsContent = document.getElementById("results-content");
  const formError = document.getElementById("form-error");
  const calculateBtn = document.getElementById("calculate-btn");

  // ---- config-driven dropdowns ----

  async function loadConfig() {
    let config;
    try {
      const res = await fetch("/api/config");
      config = await res.json();
    } catch (err) {
      showFormError("Couldn't reach the local server for config: " + err);
      return;
    }

    const countySelect = document.getElementById("county-select");
    for (const county of config.counties) {
      const opt = document.createElement("option");
      opt.value = county;
      opt.textContent = county;
      countySelect.insertBefore(opt, countySelect.lastElementChild);
    }

    const dpaSelect = document.getElementById("dpa-select");
    for (const program of config.dpa_programs) {
      const opt = document.createElement("option");
      opt.value = program.key;
      opt.textContent = `${program.label} (min score ${program.min_credit_score})`;
      opt.dataset.notes = program.notes || "";
      dpaSelect.insertBefore(opt, dpaSelect.lastElementChild);
    }

    if (config.stale) {
      showFormError(
        `Config was last verified ${config.last_verified}, more than 12 months ago — rates and limits may be stale.`,
        false
      );
    }
  }

  document.getElementById("county-select").addEventListener("change", (e) => {
    const countyInput = document.getElementById("input-county");
    if (e.target.value === "__custom__") {
      countyInput.hidden = false;
      countyInput.value = "";
      countyInput.focus();
    } else {
      countyInput.hidden = true;
      countyInput.value = e.target.value;
    }
  });

  document.getElementById("dpa-select").addEventListener("change", (e) => {
    const custom = document.getElementById("dpa-custom");
    const notesEl = document.getElementById("dpa-notes");
    custom.hidden = e.target.value !== "__custom__";
    if (custom.hidden) custom.value = "";
    const opt = e.target.selectedOptions[0];
    notesEl.textContent = (opt && opt.dataset.notes) || "";
  });

  document.getElementById("closing-mode").addEventListener("change", (e) => {
    document.getElementById("itemized-extras").hidden = e.target.value !== "itemized";
  });

  // ---- paste / drag-drop / upload screenshot ----

  function readFileAsDataURL(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  async function handleImageFile(file) {
    if (!file || !file.type || !file.type.startsWith("image/")) return;
    const dataUrl = await readFileAsDataURL(file);
    preview.src = dataUrl;
    preview.hidden = false;
    await runOcr(dataUrl);
  }

  document.addEventListener("paste", (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.type && item.type.startsWith("image/")) {
        handleImageFile(item.getAsFile());
        e.preventDefault();
        break;
      }
    }
  });

  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files.length) handleImageFile(e.dataTransfer.files[0]);
  });
  dropzone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) handleImageFile(fileInput.files[0]);
  });

  function setOcrStatus(text, kind) {
    ocrStatus.textContent = text;
    ocrStatus.hidden = !text;
    ocrStatus.className = "status" + (kind ? " " + kind : "");
  }

  async function runOcr(dataUrl) {
    setOcrStatus("Running OCR…", "");
    calculateBtn.disabled = true;
    try {
      const res = await fetch("/api/ocr", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_base64: dataUrl }),
      });
      const data = await res.json();
      if (!res.ok) {
        setOcrStatus(data.error || "OCR failed.", "err");
        return;
      }
      applyOcrFields(data);
      setOcrStatus("Extracted values filled in below — review anything flagged before calculating.", "ok");
    } catch (err) {
      setOcrStatus("OCR request failed: " + err, "err");
    } finally {
      calculateBtn.disabled = false;
    }
  }

  function setBadge(fieldKey, confidence) {
    const badge = document.getElementById("badge-" + fieldKey);
    if (!badge) return;
    if (!confidence) {
      badge.hidden = true;
      return;
    }
    badge.hidden = false;
    badge.textContent = confidence;
    badge.className = "badge " + confidence;
    const input = document.getElementById("input-" + fieldKey);
    if (input) input.classList.toggle("flagged", confidence !== "HIGH");
  }

  function applySimpleField(key, candidate) {
    if (!candidate) return;
    setBadge(key, candidate.confidence);
    const sourceEl = document.getElementById("source-" + key);
    if (sourceEl) sourceEl.textContent = candidate.source_snippet || "";
    if (candidate.value != null) {
      document.getElementById("input-" + key).value = candidate.value;
    }
  }

  function applyOcrFields(fields) {
    // Purchase price may have several ranked candidates (§6): let the user
    // pick among them instead of only ever seeing the top guess.
    const priceSelect = document.getElementById("price-candidates");
    priceSelect.innerHTML = "";
    const candidates = fields.purchase_price_candidates || [];
    if (candidates.length > 1) {
      candidates.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.value;
        opt.textContent = `$${Number(c.value).toLocaleString()} (${c.confidence}) — ${c.source_snippet}`;
        priceSelect.appendChild(opt);
      });
      const customOpt = document.createElement("option");
      customOpt.value = "__custom__";
      customOpt.textContent = "Enter a custom value below";
      priceSelect.appendChild(customOpt);
      priceSelect.hidden = false;
      priceSelect.onchange = () => {
        if (priceSelect.value !== "__custom__") {
          document.getElementById("input-purchase_price").value = priceSelect.value;
        }
      };
    } else {
      priceSelect.hidden = true;
    }

    const best = fields.best_purchase_price || candidates[0];
    if (best && best.value != null) {
      document.getElementById("input-purchase_price").value = best.value;
      setBadge("purchase_price", best.confidence);
      document.getElementById("source-purchase_price").textContent = best.source_snippet || "";
    }

    applySimpleField("annual_property_tax", fields.annual_property_tax);
    applySimpleField("annual_hoa", fields.annual_hoa);
    applySimpleField("annual_homeowners_insurance", fields.annual_homeowners_insurance);

    if (fields.county) {
      setBadge("county", fields.county.confidence);
      document.getElementById("source-county").textContent = fields.county.source_snippet || "";
      if (fields.county.value != null) {
        document.getElementById("county-select").value = "__custom__";
        const countyInput = document.getElementById("input-county");
        countyInput.hidden = false;
        countyInput.value = fields.county.value;
      }
    }
  }

  // ---- submit / calculate ----

  function showFormError(msg, isError = true) {
    formError.textContent = msg;
    formError.hidden = !msg;
    formError.className = isError ? "error" : "status ok";
  }

  function buildRequestBody() {
    const f = form;
    const itemOverrides = {
      owners_title: f.owners_title.checked,
      radon_inspection: f.radon_inspection.checked,
      septic_inspection: f.septic_inspection.checked,
      well_inspection: f.well_inspection.checked,
      sewer_scope: f.sewer_scope.checked,
    };

    let dpa = null;
    const dpaSelectValue = document.getElementById("dpa-select").value;
    if (dpaSelectValue === "__custom__") {
      dpa = document.getElementById("dpa-custom").value || null;
    } else if (dpaSelectValue) {
      dpa = dpaSelectValue;
    }

    const countyInput = document.getElementById("input-county");
    const countySelect = document.getElementById("county-select");
    const county = (countyInput.hidden ? countySelect.value : countyInput.value) || null;

    return {
      address: document.getElementById("input-address").value || null,
      purchase_price: document.getElementById("input-purchase_price").value,
      annual_property_tax: f.annual_property_tax.value || null,
      annual_hoa: f.annual_hoa.value || null,
      annual_homeowners_insurance: f.annual_homeowners_insurance.value || null,
      county: county,
      credit_score: f.credit_score.value,
      down_pct: f.down_pct.value || null,
      rate: f.rate.value,
      term: f.term.value,
      finance_ufmip: f.finance_ufmip.value === "true",
      closing_day: f.closing_day.value,
      seller_concessions: f.seller_concessions.value || "0",
      lender_credits: f.lender_credits.value || "0",
      dpa: dpa,
      gift: f.gift.value || "0",
      earnest: f.earnest.value || "0",
      closing_mode: f.closing_mode.value,
      item_overrides: itemOverrides,
      monthly_income: f.monthly_income.value || null,
      monthly_debts: f.monthly_debts.value || null,
    };
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showFormError("");
    calculateBtn.disabled = true;
    calculateBtn.textContent = "Calculating…";

    try {
      const res = await fetch("/api/calculate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildRequestBody()),
      });
      const data = await res.json();
      if (!res.ok) {
        showFormError(data.error || "Calculation failed.");
        return;
      }
      renderResults(data);
      resultsSection.hidden = false;
      resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      showFormError("Request failed: " + err);
    } finally {
      calculateBtn.disabled = false;
      calculateBtn.textContent = "Calculate";
    }
  });

  // ---- results rendering ----

  function fmtMoney(v) {
    if (v == null) return "—";
    const n = Math.round(parseFloat(v));
    return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  }

  function fmtPct(v, places = 1) {
    if (v == null) return "—";
    return (parseFloat(v) * 100).toFixed(places) + "%";
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : s;
    return div.innerHTML;
  }

  function rangeRow(label, est, cls = "") {
    return `<tr class="${cls}"><td>${label}</td><td>${fmtMoney(est.low)}</td><td>${fmtMoney(est.likely)}</td><td>${fmtMoney(est.high)}</td></tr>`;
  }

  function flatRow(label, value, cls = "") {
    return `<tr class="${cls}"><td>${label}</td><td>${fmtMoney(value)}</td></tr>`;
  }

  function renderResults(data) {
    let html = "";

    if (data.notes && data.notes.length) {
      html += `<ul class="notes">${data.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`;
    }

    html += `<div class="result-block">
      <h3>Property</h3>
      <p class="property-line"><span>${escapeHtml(data.property_inputs.address || "(no address)")}</span><span>${fmtMoney(data.property_inputs.purchase_price)}</span></p>
    </div>`;

    const c = data.cash_to_close;

    let closingLabel = "Closing costs";
    if (data.closing.mode === "percentage") {
      const lowPct = ((parseFloat(data.closing.estimate.low) / parseFloat(data.loan.base_loan_amount)) * 100).toFixed(1);
      const highPct = ((parseFloat(data.closing.estimate.high) / parseFloat(data.loan.base_loan_amount)) * 100).toFixed(1);
      closingLabel = `Closing costs (${lowPct}–${highPct}% of loan)`;
    } else {
      closingLabel = `Closing costs (itemized, ${data.closing.line_items.length} items)`;
    }

    let ufmipRow;
    if (data.mip.ufmip_financed) {
      ufmipRow = "<tr><td>Upfront MIP</td><td>financed</td><td>financed</td><td>financed</td></tr>";
    } else {
      ufmipRow = rangeRow("Upfront MIP", c.ufmip_cash_component);
    }

    let creditRows = rangeRow("Less: seller concessions", c.seller_concessions_applied);
    if (parseFloat(data.credits.lender_credits) > 0) {
      const v = data.credits.lender_credits;
      creditRows += `<tr><td>Less: lender credits</td><td>${fmtMoney(v)}</td><td>${fmtMoney(v)}</td><td>${fmtMoney(v)}</td></tr>`;
    }
    if (parseFloat(data.credits.dpa_amount) > 0) {
      const v = data.credits.dpa_amount;
      const label = data.credits.dpa_program_label ? `Less: DPA (${escapeHtml(data.credits.dpa_program_label)})` : "Less: DPA";
      creditRows += `<tr><td>${label}</td><td>${fmtMoney(v)}</td><td>${fmtMoney(v)}</td><td>${fmtMoney(v)}</td></tr>`;
    }

    const tier = parseInt(data.buyer.credit_score, 10) >= 580 ? "580+" : "500-579";
    const downLabel = `Down payment (${fmtPct(data.loan.down_payment_pct)}, ${tier} tier)`;

    html += `<div class="result-block">
      <h3>Cash to close</h3>
      <table class="result-table">
        <thead><tr><th>Item</th><th>Low</th><th>Likely</th><th>High</th></tr></thead>
        <tbody>
          ${rangeRow(downLabel, c.down_payment)}
          ${rangeRow(closingLabel, c.closing_costs)}
          ${rangeRow("Prepaids &amp; escrow setup", c.prepaids_and_escrow)}
          ${ufmipRow}
          ${creditRows}
          ${rangeRow("TOTAL CASH NEEDED", c.total_cash_needed, "total")}
          ${rangeRow("of which from your own savings", c.cash_from_own_savings, "sub")}
        </tbody>
      </table>
    </div>`;

    let extraLines = "";
    if (parseFloat(data.credits.earnest_money_already_paid) > 0) {
      extraLines += `<p class="hint-inline">Earnest money (due at contract, credited back): ${fmtMoney(
        data.credits.earnest_money_already_paid
      )} — needed earlier, not part of the total above.</p>`;
    }
    extraLines += `<p class="hint-inline">Suggested reserve (${c.reserve.months_piti} mo PITI + moving, not required): ${fmtMoney(
      c.reserve.total
    )}</p>`;
    html += `<div class="result-block">${extraLines}</div>`;

    const m = data.monthly;
    const duration = data.mip.mip_duration_years == null ? "life of loan" : `${data.mip.mip_duration_years} years`;
    html += `<div class="result-block">
      <h3>Estimated monthly payment</h3>
      <table class="result-table">
        <tbody>
          ${flatRow("Principal &amp; interest", m.principal_and_interest)}
          ${flatRow(`MIP (${fmtPct(data.mip.annual_mip_rate, 2)}/yr, ${duration})`, m.mip)}
          ${flatRow("Property tax", m.property_tax)}
          ${flatRow("Homeowners insurance", m.homeowners_insurance)}
          ${flatRow("HOA", m.hoa)}
          ${flatRow("TOTAL", m.total, "total")}
        </tbody>
      </table>
    </div>`;

    if (data.dti) {
      html += `<div class="result-block">
        <h3>Debt-to-income <span class="optional">(informational only, not an approval prediction)</span></h3>
        <p>${fmtPct(data.dti.back_end_dti)} back-end DTI (target ≤ ${fmtPct(data.dti.target_max)})</p>
      </div>`;
    }

    resultsContent.innerHTML = html;
  }

  loadConfig();
})();
