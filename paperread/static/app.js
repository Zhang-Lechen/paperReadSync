"use strict";

// Presentation only: this file never sends requests or starts reading jobs.
const panels = [...document.querySelectorAll(".sync-card")];
const ledgerRows = [...document.querySelectorAll("[data-entry-target]")];
const filterRows = [...document.querySelectorAll("[data-ledger-state]")];
const ledgerFilter = document.getElementById("ledger-filter");
const viewStorageKey = "paperread:ledger-view:" + document.body.dataset.viewScope;

function saveLedgerView() {
  const view = {
    filter: ledgerFilter?.value,
    panelId: panels.find(panel => !panel.hidden)?.id,
    details: Object.fromEntries([...document.querySelectorAll("details[id]")]
      .map(detail => [detail.id, detail.open])),
    hash: location.hash,
    scrollY: window.scrollY
  };
  try { sessionStorage.setItem(viewStorageKey, JSON.stringify(view)); } catch { /* Storage may be disabled. */ }
}

function readLedgerView() {
  try { return JSON.parse(sessionStorage.getItem(viewStorageKey)); } catch { return null; }
}

function filterLedger(value, updateUrl = true) {
  if (!ledgerFilter) return;
  ledgerFilter.value = value;
  let visible = 0;
  for (const row of filterRows) {
    row.hidden = value !== "all" && row.dataset.ledgerState !== value;
    if (!row.hidden) visible++;
    if (row.hidden && row.dataset.entryTarget) {
      const panel = document.getElementById(row.dataset.entryTarget);
      if (panel && !panel.hidden) showEntry(null);
    }
  }
  document.getElementById("ledger-count").textContent = `${visible} / ${filterRows.length} 条`;
  document.getElementById("ledger-empty").hidden = visible > 0;
  if (updateUrl) {
    const url = new URL(location.href);
    url.searchParams.set("ledger", value);
    // A filtered-out entry must not reopen through a stale hash.
    const target = document.getElementById(url.hash.slice(1))?.closest(".sync-card");
    if (target && ledgerRows.find(row => row.dataset.entryTarget === target.id)?.hidden) url.hash = "";
    history.replaceState(null, "", url);
  }
}

if (ledgerFilter) {
  document.getElementById("apply-ledger-filter").hidden = true;
  ledgerFilter.addEventListener("change", () => filterLedger(ledgerFilter.value));
}

function showEntry(panel, target = panel, scroll = false) {
  for (const candidate of panels) candidate.hidden = candidate !== panel;
  for (const row of ledgerRows) {
    const selected = panel && row.dataset.entryTarget === panel.id;
    row.classList.toggle("is-selected", Boolean(selected));
    row.querySelector(".entry-link")?.setAttribute("aria-expanded", String(Boolean(selected)));
  }
  if (!panel) return;
  for (let ancestor = target.parentElement; ancestor && ancestor !== panel; ancestor = ancestor.parentElement) {
    if (ancestor.tagName === "DETAILS") ancestor.open = true;
  }
  if (target.tagName === "DETAILS") target.open = true;
  if (scroll) target.scrollIntoView({ block: "start", behavior: "smooth" });
}

function openDialog(dialog) {
  if (dialog && !dialog.open) dialog.showModal();
}

function followHash(scroll = false) {
  const queryFilter = new URL(location.href).searchParams.get("ledger") || "unfinished";
  filterLedger(["unfinished", "completed", "all"].includes(queryFilter) ? queryFilter : "unfinished", false);
  let id;
  try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
  const target = document.getElementById(id);
  if (target?.tagName === "DIALOG") {
    openDialog(target);
    return;
  }
  const panel = target?.closest(".sync-card");
  const row = panel && ledgerRows.find(candidate => candidate.dataset.entryTarget === panel.id);
  if (row?.hidden) filterLedger(row.dataset.ledgerState);
  showEntry(panel || null, target, scroll);
  if (target?.tagName === "DETAILS") target.open = true;
}

for (const button of document.querySelectorAll("[data-open-dialog]")) {
  button.addEventListener("click", () => openDialog(document.getElementById(button.dataset.openDialog)));
}
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.querySelector("[data-close-dialog]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
  });
  dialog.addEventListener("close", () => {
    if (location.hash === "#" + dialog.id) history.replaceState(null, "", location.pathname + location.search);
  });
}
for (const row of ledgerRows) {
  row.addEventListener("click", event => {
    if (event.target.closest("button, input, select, form")) return;
    const link = event.target.closest("a");
    if (link && !link.classList.contains("entry-link")) return;
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const panel = document.getElementById(row.dataset.entryTarget);
    if (panel && !panel.hidden) {
      history.pushState(null, "", location.pathname + location.search);
      showEntry(null);
    } else {
      location.hash = row.dataset.entryTarget;
      showEntry(panel, panel, true);
    }
  });
}
for (const button of document.querySelectorAll("[data-close-entry]")) {
  button.addEventListener("click", () => {
    const panel = button.closest(".sync-card");
    history.pushState(null, "", location.pathname + location.search);
    showEntry(null);
    document.querySelector(`[aria-controls="${panel.id}"]`)?.focus();
  });
}
window.addEventListener("hashchange", () => followHash(true));
window.addEventListener("popstate", () => followHash(true));

function restoreLedgerView() {
  const saved = readLedgerView();
  if (!saved || typeof saved !== "object") {
    followHash(Boolean(location.hash));
    return;
  }
  for (const detail of document.querySelectorAll("details[id]")) {
    if (typeof saved.details?.[detail.id] === "boolean") detail.open = saved.details[detail.id];
  }

  const url = new URL(location.href);
  const sameLocation = !url.hash || url.hash === saved.hash;
  if (!url.searchParams.has("ledger") && ["unfinished", "completed", "all"].includes(saved.filter)) {
    filterLedger(saved.filter, false);
    const destination = new URL(location.href);
    destination.searchParams.set("ledger", saved.filter);
    history.replaceState(null, "", destination);
  }
  if (!url.hash && typeof saved.panelId === "string") {
    const panel = document.getElementById(saved.panelId);
    const row = ledgerRows.find(candidate => candidate.dataset.entryTarget === saved.panelId);
    if (panel && row?.hidden && !url.searchParams.has("ledger")) {
      // The operation may have moved this entry from unfinished to completed.
      filterLedger(row.dataset.ledgerState, false);
    }
    if (panel && row && !row.hidden) {
      const destination = new URL(location.href);
      destination.searchParams.set("ledger", ledgerFilter.value);
      destination.hash = panel.id;
      history.replaceState(null, "", destination);
    }
  }
  const restoreScroll = sameLocation && ledgerFilter?.value === saved.filter &&
    !url.searchParams.has("notice") && !url.searchParams.has("search") &&
    Number.isFinite(saved.scrollY) && saved.scrollY >= 0;
  followHash(!restoreScroll && Boolean(location.hash));
  if (restoreScroll) {
    // Wait until restored folds and the browser's fragment positioning have settled.
    requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo({ top: saved.scrollY, behavior: "instant" })));
  }
}

document.addEventListener("submit", saveLedgerView);
window.addEventListener("pagehide", saveLedgerView);
restoreLedgerView();
