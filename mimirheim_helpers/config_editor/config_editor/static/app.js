/**
 * mimirheim config editor -- frontend logic (Jedison-based, plan 69).
 *
 * Every entry (mimirheim.yaml itself and every helper) is rendered through
 * the vendored Jedison library against the registry-backed API
 * (GET /api/registry, GET /api/entry/<id>, POST /api/save,
 * POST /api/preview, POST /api/reload -- SPEC.md §12). This file owns:
 *
 *   - the nav rail (one item per registry entry, grouped by category)
 *   - constructing exactly one Jedison instance per entry, only when its
 *     tab is opened (SPEC.md §5)
 *   - the enabled/disabled toggle per entry (SPEC.md §9) and the
 *     exclusive-group deletion warning (SPEC.md §8)
 *   - collecting the dirty set and driving save/preview
 *   - Reports mode and the #helper=<filename> / #<Label> deep-link migration
 *
 * No build step, no external dependencies beyond the vendored Jedison UMD
 * build and static/topic-placeholder-editor.js.
 */

"use strict";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CATEGORY_ORDER = ["core", "prices", "pv", "baseload", "scheduling", "reporting", "other"];
const CATEGORY_LABELS = {
  core: "Core",
  prices: "Prices",
  pv: "PV",
  baseload: "Baseload",
  scheduling: "Scheduling",
  reporting: "Reporting",
  other: "Other",
};

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

/** @type {{entries: Object, problems: Array}|null} Last GET /api/registry payload. */
let gRegistry = null;

/** @type {object|null} Shared Jedison.Theme instance, reused across entries. */
let gTheme = null;

/** @type {Array<Function>} customEditors passed to every Jedison.Create call. */
let gCustomEditors = [];

/**
 * One entry per opened id: {id, jedison, container, navButton, schema,
 * initialEnabled, enabledNow, mqttEnv}. An entry present here always has a
 * constructed Jedison instance (SPEC.md §5) -- a disabled, unopened entry is
 * never in this map.
 * @type {Map<string, object>}
 */
const gOpen = new Map();

/** @type {string|null} The id of the currently visible entry. */
let gCurrentEntryId = null;

/** @type {'config'|'reports'} */
let gMode = "config";

/** @type {boolean} */
let gReportsAvailable = false;

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

/**
 * Turn a registry id into a readable fallback nav label, used until the
 * entry is opened and its schema's real "title" is known.
 * @param {string} id
 * @returns {string}
 */
function humanizeId(id) {
  return id
    .split("-")
    .map((word) => (word ? word.charAt(0).toUpperCase() + word.slice(1) : word))
    .join(" ");
}

/**
 * Walk a Jedison instance tree and reset every isDirty flag to false.
 *
 * Must run immediately after constructing an entry's instance from its
 * freshly-loaded value, before rendering it to the user (SPEC.md §5): a
 * field using x-watch/x-template forces setValue() at construction, which
 * unconditionally sets isDirty = true, and Jedison exposes no reset of its
 * own (IMPLEMENTATION_DETAILS.md).
 *
 * @param {object} instance A Jedison Instance (e.g. jedison.root).
 */
function resetDirtyRecursive(instance) {
  if (!instance) return;
  instance.isDirty = false;
  for (const child of instance.children || []) resetDirtyRecursive(child);
}

/**
 * Sorted list of [entryId, entry] pairs from a GET /api/registry payload,
 * ordered by category (SPEC.md §3's closed set, in CATEGORY_ORDER), then by
 * x-mimirheim.order, then by id for a stable tiebreak.
 * @param {object} registry
 * @returns {Array<[string, object]>}
 */
function sortedRegistryEntries(registry) {
  const pairs = Object.entries(registry.entries || {});
  pairs.sort((a, b) => {
    const envA = a[1]["x-mimirheim"];
    const envB = b[1]["x-mimirheim"];
    const catA = CATEGORY_ORDER.indexOf(envA.category);
    const catB = CATEGORY_ORDER.indexOf(envB.category);
    if (catA !== catB) return catA - catB;
    if (envA.order !== envB.order) return envA.order - envB.order;
    return a[0].localeCompare(b[0]);
  });
  return pairs;
}

/**
 * Remove the injected, read-only "context" key (SPEC.md §5) from a value
 * fetched via jedison.getValue(), which must never be written back to disk.
 * @param {object} value
 * @returns {object}
 */
function stripContext(value) {
  const { context, ...rest } = value || {};
  return rest;
}

// ---------------------------------------------------------------------------
// Nav rail
// ---------------------------------------------------------------------------

/**
 * (Re)build the nav rail from the current gRegistry. Existing open entries'
 * state is preserved -- only the nav DOM is rebuilt (called on load and
 * after POST /api/reload).
 */
function buildNav() {
  const nav = document.getElementById("entry-nav");
  nav.innerHTML = "";

  if (gRegistry.problems && gRegistry.problems.length > 0) {
    const banner = document.createElement("details");
    banner.className = "nav-problems";
    const summary = document.createElement("summary");
    summary.textContent = `${gRegistry.problems.length} schema(s) failed to load`;
    banner.appendChild(summary);
    const list = document.createElement("ul");
    for (const problem of gRegistry.problems) {
      const item = document.createElement("li");
      item.textContent = `${problem.source}: ${problem.reason}`;
      list.appendChild(item);
    }
    banner.appendChild(list);
    nav.appendChild(banner);
  }

  let lastCategory = null;
  for (const [id, entry] of sortedRegistryEntries(gRegistry)) {
    const category = entry["x-mimirheim"].category;
    if (category !== lastCategory) {
      const heading = document.createElement("div");
      heading.className = "nav-category";
      heading.textContent = CATEGORY_LABELS[category] || category;
      nav.appendChild(heading);
      lastCategory = category;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "nav-item";
    button.dataset.entryId = id;
    const open = gOpen.get(id);
    button.textContent = (open && open.schema && open.schema.title) || humanizeId(id);
    if (!entry.enabled && !entry["x-mimirheim"].required && !open) {
      button.classList.add("nav-item-disabled");
    }
    button.addEventListener("click", () => openEntry(id));
    nav.appendChild(button);
  }

  updateNavActiveState();
  for (const id of gOpen.keys()) updateNavIndicators(id);
}

/** Mark the current entry's nav button active; clear the rest. */
function updateNavActiveState() {
  const nav = document.getElementById("entry-nav");
  nav.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.entryId === gCurrentEntryId);
  });
}

/**
 * Refresh one entry's nav button: real title (once known), dirty dot,
 * validation-invalid badge, disabled/enabled styling.
 * @param {string} id
 */
function updateNavIndicators(id) {
  const nav = document.getElementById("entry-nav");
  const button = nav.querySelector(`.nav-item[data-entry-id="${CSS.escape(id)}"]`);
  if (!button) return;
  const open = gOpen.get(id);
  const regEntry = gRegistry.entries[id];

  button.textContent = (open && open.schema && open.schema.title) || humanizeId(id);
  button.classList.toggle(
    "nav-item-disabled",
    !!regEntry && !regEntry.enabled && !regEntry["x-mimirheim"].required && !open
  );

  let dot = button.querySelector(".nav-item-dirty-dot");
  const isDirty = !!open && entryHasPendingChange(open);
  if (isDirty && !dot) {
    dot = document.createElement("span");
    dot.className = "nav-item-dirty-dot";
    dot.title = "Unsaved changes";
    button.appendChild(dot);
  } else if (!isDirty && dot) {
    dot.remove();
  }

  let warn = button.querySelector(".jedi-nav-warning-dot");
  const hasErrors = !!open && open.jedison.getErrors().length > 0;
  if (hasErrors && !warn) {
    warn = document.createElement("span");
    warn.className = "jedi-nav-warning-dot";
    warn.title = "Validation errors";
    button.appendChild(warn);
  } else if (!hasErrors && warn) {
    warn.remove();
  }
}

// ---------------------------------------------------------------------------
// Entry construction (SPEC.md §5) and enabled/disabled placeholder (SPEC.md §9)
// ---------------------------------------------------------------------------

/**
 * The single, shared entry point for turning a fetched {schema, value} pair
 * into a mounted, dirty-reset Jedison instance (Decision 4/5). There is
 * exactly one call site for this so RefParser dereferencing and the isDirty
 * reset can never be skipped by a future call site added carelessly.
 *
 * @param {object} schema Entry schema, not yet dereferenced.
 * @param {object} value  Entry's current (or default) value.
 * @param {HTMLElement} container Element to render into.
 * @returns {Promise<object>} The constructed Jedison.Create instance.
 */
async function constructInstance(schema, value, container) {
  const refParser = new Jedison.RefParser({ fetch: undefined });
  await refParser.dereference(schema);
  const jedison = new Jedison.Create({
    schema,
    data: value,
    refParser,
    container,
    theme: gTheme,
    customEditors: gCustomEditors,
  });
  resetDirtyRecursive(jedison.root);
  return jedison;
}

/**
 * Open an entry: construct its Jedison instance on first open (fetching
 * GET /api/entry/<id>), or just reveal it if already open this session.
 * A disabled, non-required entry with no open instance shows the "not
 * enabled" placeholder instead, per SPEC.md §9 -- no instance is constructed
 * for it until the user clicks "Enable".
 *
 * @param {string} id
 */
async function openEntry(id) {
  gCurrentEntryId = id;
  location.hash = "";
  const url = new URL(location.href);
  url.searchParams.set("entry", id);
  history.replaceState(null, "", url);
  updateNavActiveState();

  const regEntry = gRegistry.entries[id];
  if (!regEntry) return;

  if (!gOpen.has(id)) {
    if (!regEntry.enabled && !regEntry["x-mimirheim"].required) {
      renderDisabledPlaceholder(id);
      return;
    }
    await loadAndMountEntry(id);
  }
  showEntryPane(id);
}

/**
 * Fetch GET /api/entry/<id>, construct its instance, and mount it (hidden
 * until shown by showEntryPane).
 * @param {string} id
 */
async function loadAndMountEntry(id) {
  const resp = await fetch(`api/entry/${id}`);
  const data = await resp.json();

  const pane = document.createElement("div");
  pane.className = "entry-pane";
  pane.hidden = true;

  const chrome = buildEntryChrome(id, data.enabled);
  pane.appendChild(chrome);

  if (data.mqtt_env && Object.keys(data.mqtt_env).length > 0) {
    pane.appendChild(buildMqttEnvBanner(data.mqtt_env));
  }

  const formHost = document.createElement("div");
  pane.appendChild(formHost);
  document.getElementById("entry-content").appendChild(pane);

  const jedison = await constructInstance(data.schema, data.value, formHost);

  const entryState = {
    id,
    jedison,
    pane,
    formHost,
    schema: data.schema,
    initialEnabled: data.enabled,
    enabledNow: true,
  };
  gOpen.set(id, entryState);

  jedison.on("instance-change", () => updateNavIndicators(id));
  updateNavIndicators(id);
}

/**
 * Build the small chrome row above an entry's rendered form: enable/disable
 * toggle (hidden for required entries) and a docs link if present.
 * @param {string} id
 * @param {boolean} enabled
 * @returns {HTMLElement}
 */
function buildEntryChrome(id, enabled) {
  const regEntry = gRegistry.entries[id];
  const row = document.createElement("div");
  row.className = "entry-chrome";

  if (!regEntry["x-mimirheim"].required) {
    const label = document.createElement("label");
    label.className = "entry-enable-toggle";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = enabled;
    checkbox.addEventListener("change", () => {
      const entryState = gOpen.get(id);
      entryState.enabledNow = checkbox.checked;
      entryState.formHost.hidden = !checkbox.checked;
      updateNavIndicators(id);
    });
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode("Enabled"));
    row.appendChild(label);
  }

  const docsUrl = regEntry["x-mimirheim"].docs_url;
  if (docsUrl) {
    const link = document.createElement("a");
    link.href = docsUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Documentation";
    link.className = "entry-docs-link";
    row.appendChild(link);
  }

  return row;
}

/**
 * A small, generic info banner listing which mqtt fields the HA Supervisor
 * supplies for this entry -- ported from the pre-Jedison frontend's MQTT env
 * handling, simplified: the new backend's GET /api/entry response never
 * contains a real credential value (server.py's mqtt_env is pre-redacted),
 * so no per-field placeholder trickery is needed here; leaving a
 * Supervisor-supplied field blank already works end to end via
 * POST /api/save's existing env-merge-for-validation-only logic.
 *
 * @param {object} mqttEnv e.g. {host: "...", password: "__supervisor_provided__"}
 * @returns {HTMLElement}
 */
function buildMqttEnvBanner(mqttEnv) {
  const banner = document.createElement("div");
  banner.className = "info-banner";
  const fields = Object.keys(mqttEnv).sort().join(", ");
  banner.textContent =
    `MQTT connection settings (${fields}) are supplied by the Home Assistant ` +
    "Supervisor. Leave this entry's mqtt fields blank to use them; only fill " +
    "them in if this instance needs different broker settings.";
  return banner;
}

/**
 * Render the "not enabled" placeholder for a disabled, non-required entry
 * (SPEC.md §9) -- no Jedison instance exists for it yet.
 * @param {string} id
 */
function renderDisabledPlaceholder(id) {
  hideAllPanes();
  const content = document.getElementById("entry-content");
  let placeholder = content.querySelector(`.entry-placeholder[data-entry-id="${CSS.escape(id)}"]`);
  if (!placeholder) {
    placeholder = document.createElement("div");
    placeholder.className = "entry-placeholder";
    placeholder.dataset.entryId = id;
    const message = document.createElement("p");
    message.textContent = `${humanizeId(id)} is not enabled.`;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Enable";
    button.addEventListener("click", async () => {
      await loadAndMountEntry(id);
      showEntryPane(id);
    });
    placeholder.appendChild(message);
    placeholder.appendChild(button);
    content.appendChild(placeholder);
  }
  placeholder.hidden = false;
}

/** Hide every mounted entry pane and disabled-entry placeholder. */
function hideAllPanes() {
  const content = document.getElementById("entry-content");
  content.querySelectorAll(".entry-pane, .entry-placeholder").forEach((el) => {
    el.hidden = true;
  });
}

/**
 * Show one entry's pane (constructed form, or the disabled placeholder if
 * the user has toggled it off since opening).
 * @param {string} id
 */
function showEntryPane(id) {
  hideAllPanes();
  const loading = document.getElementById("loading-msg");
  if (loading) loading.remove();
  const entryState = gOpen.get(id);
  if (!entryState) return;
  entryState.pane.hidden = false;
  entryState.formHost.hidden = !entryState.enabledNow;
}

// ---------------------------------------------------------------------------
// Save / preview (SPEC.md §8)
// ---------------------------------------------------------------------------

/**
 * Whether an opened entry represents a change worth including in a save:
 * newly enabled, newly disabled, or edited since load (isDirty, after the
 * post-construction reset -- SPEC.md §5/§8).
 * @param {object} entryState
 * @returns {boolean}
 */
function entryHasPendingChange(entryState) {
  if (entryState.enabledNow !== entryState.initialEnabled) return true;
  return entryState.enabledNow && entryState.jedison.root.isDirty;
}

/**
 * Build the {"entries": {...}} payload SPEC.md §12 describes for
 * POST /api/save and POST /api/preview, from every opened entry with a
 * pending change. Entries never opened this session are never touched
 * (SPEC.md §8's dirty set).
 * @returns {object}
 */
function buildSavePayload() {
  const entries = {};
  for (const [id, entryState] of gOpen) {
    if (!entryHasPendingChange(entryState)) continue;
    entries[id] = entryState.enabledNow
      ? { enabled: true, config: stripContext(entryState.jedison.getValue()) }
      : { enabled: false };
  }
  return { entries };
}

/**
 * For every entry the payload newly enables, find sibling files sharing its
 * exclusive_group that are currently enabled on the server and are not
 * themselves being kept enabled in this same save -- these will be deleted
 * as part of the transactional save (SPEC.md §8). The user must be warned
 * before confirming.
 * @param {object} payload {"entries": {...}}
 * @returns {string[]} Bare filenames that would be deleted as a side effect.
 */
function exclusiveGroupDeletions(payload) {
  const deletions = new Set();
  for (const [id, spec] of Object.entries(payload.entries)) {
    if (!spec.enabled) continue;
    const group = gRegistry.entries[id]["x-mimirheim"].exclusive_group;
    if (!group) continue;
    for (const [otherId, otherEntry] of Object.entries(gRegistry.entries)) {
      if (otherId === id || otherEntry["x-mimirheim"].exclusive_group !== group) continue;
      const keptEnabled = payload.entries[otherId] && payload.entries[otherId].enabled;
      if (otherEntry.enabled && !keptEnabled) deletions.add(otherEntry["x-mimirheim"].file);
    }
  }
  return [...deletions];
}

/**
 * Show a blocking confirmation modal; resolves true/false with the user's
 * choice.
 * @param {string} message
 * @returns {Promise<boolean>}
 */
function confirmModal(message) {
  return new Promise((resolve) => {
    const root = document.getElementById("modal-root");
    root.innerHTML = "";
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const dialog = document.createElement("div");
    dialog.className = "modal-dialog";
    const text = document.createElement("p");
    text.textContent = message;
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.textContent = "Confirm";
    confirm.className = "modal-confirm-danger";
    const finish = (result) => {
      root.innerHTML = "";
      resolve(result);
    };
    cancel.addEventListener("click", () => finish(false));
    confirm.addEventListener("click", () => finish(true));
    actions.appendChild(cancel);
    actions.appendChild(confirm);
    dialog.appendChild(text);
    dialog.appendChild(actions);
    overlay.appendChild(dialog);
    root.appendChild(overlay);
  });
}

/**
 * Show a panel of preview diffs.
 *
 * In view mode (`confirmable` false, the standalone Preview button) it has a
 * single Close button and always resolves true. In confirmable mode (the
 * review-changes step gating a save, Decision 3) it has Cancel/Confirm save
 * buttons and resolves with the user's choice -- this is what makes a
 * save-time key deletion (SPEC.md §8) visible before it happens, not just
 * discoverable via the separate Preview button.
 * @param {object} diffs
 * @param {boolean} [confirmable=false]
 * @returns {Promise<boolean>}
 */
function showPreviewDiffs(diffs, confirmable = false) {
  return new Promise((resolve) => {
    const root = document.getElementById("modal-root");
    root.innerHTML = "";
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const dialog = document.createElement("div");
    dialog.className = "modal-dialog modal-dialog-wide";
    const heading = document.createElement("h2");
    heading.textContent = confirmable ? "Review changes" : "Preview";
    dialog.appendChild(heading);
    const filenames = Object.keys(diffs);
    if (filenames.length === 0) {
      const empty = document.createElement("p");
      empty.textContent = "No changes to preview.";
      dialog.appendChild(empty);
    }
    for (const filename of filenames) {
      const section = document.createElement("section");
      const title = document.createElement("h3");
      title.textContent = filename;
      const pre = document.createElement("pre");
      pre.textContent = diffs[filename];
      section.appendChild(title);
      section.appendChild(pre);
      dialog.appendChild(section);
    }
    const finish = (result) => {
      root.innerHTML = "";
      resolve(result);
    };
    if (confirmable) {
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "Cancel";
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.textContent = "Confirm save";
      cancel.addEventListener("click", () => finish(false));
      confirm.addEventListener("click", () => finish(true));
      actions.appendChild(cancel);
      actions.appendChild(confirm);
      dialog.appendChild(actions);
    } else {
      const close = document.createElement("button");
      close.type = "button";
      close.textContent = "Close";
      close.addEventListener("click", () => finish(true));
      dialog.appendChild(close);
    }
    overlay.appendChild(dialog);
    root.appendChild(overlay);
  });
}

/**
 * POST payload to /api/preview and return the parsed response.
 * @param {object} payload {"entries": {...}}
 * @returns {Promise<object>} `{"ok": true, "diffs": {...}} | {"ok": false, "errors": {...}}`
 */
async function fetchPreview(payload) {
  const resp = await fetch("api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return resp.json();
}

/** Render per-entry save/preview errors into the status area. @param {object} errors */
function showSaveErrors(errors) {
  let banner = document.getElementById("error-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "error-banner";
    banner.className = "error-banner";
    document.getElementById("entry-content").prepend(banner);
  }
  const items = [];
  for (const [id, entryErrors] of Object.entries(errors)) {
    for (const err of entryErrors) {
      const loc = (err.loc || []).join(".");
      items.push(`${id}${loc ? "." + loc : ""}: ${err.msg}`);
    }
  }
  banner.innerHTML = "";
  const strong = document.createElement("strong");
  strong.textContent = "Validation errors:";
  const list = document.createElement("ul");
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    list.appendChild(li);
  }
  banner.appendChild(strong);
  banner.appendChild(list);
}

function clearSaveErrors() {
  const banner = document.getElementById("error-banner");
  if (banner) banner.remove();
}

async function handlePreview() {
  const status = document.getElementById("save-status");
  const payload = buildSavePayload();
  if (Object.keys(payload.entries).length === 0) {
    status.textContent = "Nothing to preview.";
    setTimeout(() => {
      status.textContent = "";
    }, 3000);
    return;
  }
  const data = await fetchPreview(payload);
  if (!data.ok) {
    showSaveErrors(data.errors || {});
    return;
  }
  await showPreviewDiffs(data.diffs || {});
}

async function handleSave() {
  const btn = document.getElementById("save-btn");
  const status = document.getElementById("save-status");
  const payload = buildSavePayload();

  if (Object.keys(payload.entries).length === 0) {
    status.textContent = "Nothing to save.";
    setTimeout(() => {
      status.textContent = "";
    }, 3000);
    return;
  }

  const deletions = exclusiveGroupDeletions(payload);
  if (deletions.length > 0) {
    const proceed = await confirmModal(
      `Saving will delete ${deletions.join(", ")} (mutually exclusive with an entry you are enabling). Continue?`
    );
    if (!proceed) return;
  }

  clearSaveErrors();
  const previewData = await fetchPreview(payload);
  if (!previewData.ok) {
    showSaveErrors(previewData.errors || {});
    status.textContent = "Validation errors — nothing was saved.";
    return;
  }
  const confirmed = await showPreviewDiffs(previewData.diffs || {}, true);
  if (!confirmed) return;

  btn.disabled = true;
  status.textContent = "Saving…";
  clearSaveErrors();
  try {
    const resp = await fetch("api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.ok) {
      status.textContent = "Saved.";
      for (const [id, entryState] of gOpen) {
        if (!payload.entries[id]) continue;
        entryState.initialEnabled = entryState.enabledNow;
        if (entryState.enabledNow) resetDirtyRecursive(entryState.jedison.root);
        updateNavIndicators(id);
      }
      await reloadRegistry();
      setTimeout(() => {
        status.textContent = "";
      }, 3000);
    } else {
      showSaveErrors(data.errors || {});
      status.textContent = "Validation errors — nothing was saved.";
    }
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

/** Re-run POST /api/reload and rebuild the nav to reflect any file-presence changes. */
async function reloadRegistry() {
  const resp = await fetch("api/reload", { method: "POST" });
  gRegistry = await resp.json();
  buildNav();
}

// ---------------------------------------------------------------------------
// Reports mode (ported from the pre-Jedison frontend, unchanged behaviour)
// ---------------------------------------------------------------------------

/**
 * Switch the UI between Reports mode (full-height iframe) and Config mode
 * (nav rail + entry content + save bar).
 * @param {'config'|'reports'} mode
 */
function setMode(mode) {
  gMode = mode;
  const reportsPane = document.getElementById("reports-pane");
  const layout = document.getElementById("config-layout");
  const saveBar = document.getElementById("save-bar");

  document.getElementById("mode-btn-reports").classList.toggle("active", mode === "reports");
  document.getElementById("mode-btn-config").classList.toggle("active", mode === "config");

  if (mode === "reports") {
    layout.style.display = "none";
    saveBar.style.display = "none";
    reportsPane.style.display = "block";
    const headerHeight = document.querySelector("header").offsetHeight;
    reportsPane.style.height = `calc(100vh - ${headerHeight}px)`;
    reportsPane.innerHTML = "";
    if (gReportsAvailable) {
      const iframe = document.createElement("iframe");
      iframe.id = "reports-frame";
      iframe.src = "reports/";
      reportsPane.appendChild(iframe);
    } else {
      const msg = document.createElement("p");
      msg.className = "reports-unavailable";
      msg.textContent =
        "No reports available. Set reports_dir in config-editor.yaml to the " +
        "reporting helper's output_dir and restart the config editor.";
      reportsPane.appendChild(msg);
    }
  } else {
    reportsPane.style.display = "none";
    reportsPane.innerHTML = "";
    layout.style.display = "";
    saveBar.style.display = "";
  }
}

// ---------------------------------------------------------------------------
// Deep linking (Decision 8)
// ---------------------------------------------------------------------------

/**
 * Resolve the page's initial target entry/mode from the URL: the current
 * "?entry=<id>" scheme, or the pre-Jedison frontend's "#helper=<filename>"
 * fragment (Decision 8) -- resolved generically by matching the filename
 * against the just-fetched registry's own x-mimirheim.file, never a
 * hardcoded id/filename table (an id need not equal its target filename's
 * stem, SPEC.md §1).
 * @returns {{mode: 'config'|'reports', entryId: string|null}}
 */
function resolveDeepLink() {
  const params = new URLSearchParams(location.search);
  const entryParam = params.get("entry");
  if (entryParam && gRegistry.entries[entryParam]) {
    return { mode: "config", entryId: entryParam };
  }

  const hash = location.hash.slice(1);
  if (hash === "reports") return { mode: "reports", entryId: null };

  const helperMatch = /^helper=(.+)$/.exec(hash);
  if (helperMatch) {
    const filename = helperMatch[1];
    const match = Object.entries(gRegistry.entries).find(
      ([, entry]) => entry["x-mimirheim"].file === filename
    );
    if (match) return { mode: "config", entryId: match[0] };
  }

  const required = Object.entries(gRegistry.entries).find(([, e]) => e["x-mimirheim"].required);
  const [firstId] = sortedRegistryEntries(gRegistry)[0] || [null];
  return { mode: "config", entryId: (required && required[0]) || firstId };
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

async function init() {
  const registryResp = await fetch("api/registry");
  gRegistry = await registryResp.json();

  gTheme = new Jedison.Theme();
  gCustomEditors = [createMimirTopicPlaceholderEditor(Jedison)];

  // Best-effort: reports availability is inferred from whether GET /reports/
  // succeeds, without fetching its body.
  try {
    const reportsResp = await fetch("reports/", { method: "GET" });
    gReportsAvailable = reportsResp.ok;
  } catch {
    gReportsAvailable = false;
  }

  buildNav();

  const { mode, entryId } = resolveDeepLink();
  if (mode === "reports") {
    setMode("reports");
  } else if (entryId) {
    setMode("config");
    await openEntry(entryId);
  }

  document.getElementById("mode-btn-reports").addEventListener("click", () => {
    location.hash = "reports";
    setMode("reports");
  });
  document.getElementById("mode-btn-config").addEventListener("click", () => {
    setMode("config");
    if (gCurrentEntryId) showEntryPane(gCurrentEntryId);
  });

  document.getElementById("save-btn").addEventListener("click", handleSave);
  document.getElementById("preview-btn").addEventListener("click", handlePreview);

  const loading = document.getElementById("loading-msg");
  if (loading) loading.remove();
}

init().catch((err) => {
  const content = document.getElementById("entry-content");
  content.innerHTML = "";
  const p = document.createElement("p");
  p.className = "fatal-error";
  p.textContent = `Failed to load: ${err && err.message ? err.message : err}`;
  content.appendChild(p);
});
