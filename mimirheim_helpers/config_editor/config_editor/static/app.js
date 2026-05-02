/**
 * mimirheim config editor — frontend logic
 *
 * Architecture overview:
 *
 *   1. SchemaReader — fetches /api/schema on load and builds a lookup map
 *      from definition name to its JSON Schema object.
 *
 *   2. FormBuilder — given a definition name and a data dict, renders
 *      an HTML form section. Respects ui_label, ui_group, and field types.
 *
 *   3. DeviceListEditor — generic CRUD component for named-map device
 *      sections (batteries, pv_arrays, etc.). Reads field schema purely
 *      by schemaRef; no device-specific code.
 *
 *   4. Tab registration — tabs are registered at startup; the active tab
 *      is tracked via location.hash.
 *
 * No external dependencies. No build step.
 */

"use strict";

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

/** @type {Object} Parsed JSON Schema from /api/schema */
let gSchema = null;

/** @type {Object} Current config dict (last loaded from /api/config) */
let gConfig = {};

/** @type {Object} MQTT fields set by the HA Supervisor via env vars. Empty when running plain Docker. */
let gMqttEnv = {};

/** @type {boolean} Whether the reports directory is configured and has a reports index. */
let gReportsAvailable = false;

/** @type {'config'|'reports'} Current top-level display mode. */
let gMode = 'config';

/** @type {boolean} Whether the user has enabled the MQTT override in the General tab. */
let gMqttOverride = false;

/** @type {Array<{label: string, render: function}>} Registered tabs */
const gTabs = [];

// ---------------------------------------------------------------------------
// Top-level mode switcher (Reports / Config)
// ---------------------------------------------------------------------------

/**
 * Switch the UI between Reports mode (full-height iframe) and Config mode
 * (tab bar + content pane + save bar).
 *
 * In Reports mode the save bar and config sub-tabs are hidden so the iframe
 * can occupy the full remaining viewport height.
 *
 * @param {'config'|'reports'} mode
 */
function setMode(mode) {
  gMode = mode;

  const reportsPane  = document.getElementById('reports-pane');
  const tabBar       = document.getElementById('tab-bar');
  const contentPane  = document.getElementById('content-pane');
  const saveBar      = document.getElementById('save-bar');

  document.getElementById('mode-btn-reports').classList.toggle('active', mode === 'reports');
  document.getElementById('mode-btn-config').classList.toggle('active', mode === 'config');

  if (mode === 'reports') {
    tabBar.style.display      = 'none';
    contentPane.style.display = 'none';
    saveBar.style.display     = 'none';
    reportsPane.style.display = 'block';

    // Size the pane to fill the remaining viewport below the header.
    const headerH = document.querySelector('header').offsetHeight;
    reportsPane.style.height = `calc(100vh - ${headerH}px)`;

    reportsPane.innerHTML = '';
    if (gReportsAvailable) {
      const iframe = document.createElement('iframe');
      iframe.id  = 'reports-frame';
      iframe.src = 'reports/';
      reportsPane.appendChild(iframe);
    } else {
      const msg = document.createElement('p');
      msg.className   = 'reports-unavailable';
      msg.innerHTML   = 'No reports available. '
        + 'Set <code>reports_dir</code> in <code>config-editor.yaml</code> '
        + 'to the reporter <code>reporting.output_dir</code> and restart the config editor.';
      reportsPane.appendChild(msg);
    }
  } else {
    reportsPane.style.display = 'none';
    reportsPane.innerHTML     = '';
    tabBar.style.display      = '';
    contentPane.style.display = '';
    saveBar.style.display     = '';
  }
}

// ---------------------------------------------------------------------------
// Utility: resolve a $ref like "#/$defs/BatteryConfig" to its schema object
// ---------------------------------------------------------------------------

function resolveRef(ref) {
  if (!ref || !ref.startsWith("#/$defs/")) return null;
  const name = ref.slice("#/$defs/".length);
  return (gSchema.$defs || {})[name] || null;
}

/**
 * Return a copy of a helper or main config with env-supplied mqtt fields
 * merged in as defaults. File-config values take precedence over env values
 * so that explicit user overrides are preserved for display.
 *
 * @param {Object} config  Config dict (may have an mqtt sub-object or none).
 * @returns {Object}
 */
function mergedWithMqttEnv(config) {
  if (!gMqttEnv || Object.keys(gMqttEnv).length === 0) return config;
  return { ...config, mqtt: { ...gMqttEnv, ...(config.mqtt || {}) } };
}

/**
 * Strip env-supplied mqtt fields from a config dict in-place before saving.
 * A field is stripped when its value in the config matches the corresponding
 * env value, indicating the user has not overridden it. This keeps Supervisor
 * credentials out of the YAML file.
 *
 * @param {Object} config  Config dict that may contain an mqtt sub-object.
 */
function stripMqttEnvFields(config) {
  if (!gMqttEnv || Object.keys(gMqttEnv).length === 0) return;
  if (!config.mqtt) return;
  for (const [k, v] of Object.entries(gMqttEnv)) {
    // Compare after string coercion: form inputs always produce strings,
    // while env values may be bool (tls) or int (port).
    if (config.mqtt[k] === v || String(config.mqtt[k]) === String(v)) {
      delete config.mqtt[k];
    }
  }
  if (Object.keys(config.mqtt).length === 0) delete config.mqtt;
}

/**
 * Return the schema object for a named definition.
 * @param {string} defName
 * @returns {Object|null}
 */
function getDef(defName) {
  return (gSchema.$defs || {})[defName] || null;
}

// ---------------------------------------------------------------------------
// Phase 1 — Placeholder resolution
// ---------------------------------------------------------------------------

/**
 * Resolve a ui_placeholder template to a concrete topic string using the
 * current mimirheim config and a per-field context.
 *
 * Supported substitutions:
 *   {mimir_topic_prefix} / {mqtt.topic_prefix}  — mqtt.topic_prefix from gConfig
 *   {array_key} / {mimir_array_key} / {name} / {mimir_static_load_name}
 *                                               — device name from ctx.deviceName
 *
 * Returns null if the template contains a substitution that cannot be
 * resolved (e.g. no deviceName was supplied).
 *
 * @param {string}      template   The ui_placeholder string.
 * @param {{deviceName?: string}} ctx  Resolution context.
 * @returns {string|null}
 */
function resolvePlaceholder(template, ctx) {
  const prefix = (gConfig.mqtt && gConfig.mqtt.topic_prefix)
    ? gConfig.mqtt.topic_prefix
    : (gMqttEnv.topic_prefix || "mimir");

  let result = template
    .replace(/\{mimir_topic_prefix\}/g, prefix)
    .replace(/\{mqtt\.topic_prefix\}/g, prefix);

  const deviceName = ctx && ctx.deviceName;
  if (deviceName) {
    result = result
      .replace(/\{array_key\}/g, deviceName)
      .replace(/\{mimir_array_key\}/g, deviceName)
      .replace(/\{name\}/g, deviceName)
      .replace(/\{mimir_static_load_name\}/g, deviceName);
  }

  // If any template variables remain unresolved, return null.
  if (/\{[^}]+\}/.test(result)) return null;
  return result;
}

// ---------------------------------------------------------------------------
// Phase 2 — Helper prefix synchronisation
// ---------------------------------------------------------------------------

/**
 * The MQTT topic prefix from the last time topic_prefix was changed by the
 * user during this session. Used by syncHelperPrefixes to detect which
 * helpers were tracking the previous prefix (vs. intentionally diverged).
 */
let gLastKnownPrefix = null;

/**
 * Sync mimir_topic_prefix in all helper configs that are still tracking the
 * old prefix (i.e. their value matches oldPrefix). Helpers where the user
 * has already set a different custom prefix are left untouched.
 *
 * Updates gHelperConfigs in memory. The user must save each helper
 * individually to persist the change.
 *
 * Displays a dismissible banner below the MQTT section listing which helpers
 * were updated and which were skipped (diverged).
 *
 * @param {string} oldPrefix  The previous value of mqtt.topic_prefix.
 * @param {string} newPrefix  The new value of mqtt.topic_prefix.
 * @param {HTMLElement} bannerContainer  Element to render the banner into.
 */
function syncHelperPrefixes(oldPrefix, newPrefix, bannerContainer) {
  if (!gHelperConfigs || oldPrefix === newPrefix) return;

  // Helpers that carry mimir_topic_prefix at the root level.
  const prefixHelpers = [
    "nordpool.yaml",
    "pv-fetcher.yaml",
    "pv-ml-learner.yaml",
    "baseload-static.yaml",
    "baseload-ha.yaml",
    "baseload-ha-db.yaml",
    "reporter.yaml",
  ];

  const updated = [];
  const skipped = [];

  for (const fname of prefixHelpers) {
    const state = gHelperConfigs[fname];
    if (!state || !state.enabled) continue;
    const cfg = state.config || {};
    const helperPrefix = cfg.mimir_topic_prefix;
    if (helperPrefix === undefined || helperPrefix === oldPrefix) {
      // This helper was tracking the mimirheim prefix. Update it.
      if (!gHelperConfigs[fname].config) gHelperConfigs[fname].config = {};
      gHelperConfigs[fname].config.mimir_topic_prefix = newPrefix;
      updated.push(fname.replace(".yaml", ""));
    } else {
      // This helper has a custom (diverged) prefix. Leave it alone.
      skipped.push(fname.replace(".yaml", ""));
    }
  }

  // Render the banner.
  bannerContainer.innerHTML = "";
  if (updated.length === 0 && skipped.length === 0) return;

  const banner = document.createElement("div");
  banner.className = "info-banner prefix-sync-banner";

  let msg = "";
  if (updated.length > 0) {
    msg += `Topic prefix synced in: <strong>${updated.join(", ")}</strong>. "
      + "Save each helper to persist.`;
  }
  if (skipped.length > 0) {
    if (msg) msg += " ";
    msg += `Skipped (custom prefix): <strong>${skipped.join(", ")}</strong>.`;
  }
  banner.innerHTML = msg;

  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.textContent = "\u00d7";
  dismiss.style.cssText = "float:right;background:none;border:none;cursor:pointer;font-size:1.1rem;line-height:1;";
  dismiss.addEventListener("click", () => { bannerContainer.innerHTML = ""; });
  banner.prepend(dismiss);

  bannerContainer.appendChild(banner);
}

/**
 * Wire a topic_prefix change listener on an MQTT form.
 * When the user changes the topic_prefix field, calls syncHelperPrefixes()
 * and dispatches "mimirPrefixChanged" so that all open topic-derived-hint
 * elements re-evaluate.
 *
 * @param {HTMLElement} mqttForm        The form element built by buildForm("MqttConfig", …).
 * @param {HTMLElement} bannerContainer Element to render the sync banner into.
 */
function _wirePrefixSync(mqttForm, bannerContainer) {
  const prefixInput = mqttForm.querySelector("input[name='mqtt.topic_prefix']");
  if (!prefixInput) return;

  // Record the initial prefix so we can detect changes.
  gLastKnownPrefix = prefixInput.value.trim() || prefixInput.placeholder || "mimir";

  prefixInput.addEventListener("input", () => {
    const newPrefix = prefixInput.value.trim() || prefixInput.placeholder || "mimir";
    if (newPrefix === gLastKnownPrefix) return;
    syncHelperPrefixes(gLastKnownPrefix, newPrefix, bannerContainer);
    gLastKnownPrefix = newPrefix;
    // Notify all topic-derived-hint elements to re-evaluate.
    window.dispatchEvent(new CustomEvent("mimirPrefixChanged"));
  });
}

// ---------------------------------------------------------------------------
// FormBuilder
// ---------------------------------------------------------------------------

/**
 * Build an HTML form <div> for a schema definition, pre-filled from data.
 *
 * @param {string} defName  Name of the $defs entry to render.
 * @param {Object} data     Current field values.
 * @param {string} bindPath Dot-separated path for input name attributes.
 * @returns {HTMLElement}
 */
function buildForm(defName, data, bindPath) {
  const def = getDef(defName);
  if (!def) {
    const msg = document.createElement("p");
    msg.textContent = `Schema definition '${defName}' not found.`;
    return msg;
  }

  const container = document.createElement("div");
  const props = def.properties || {};
  const basicFields = [];
  const advancedFields = [];

  for (const [fieldName, fieldSchema] of Object.entries(props)) {
    const group = fieldSchema.ui_group || "advanced";
    const row = buildFieldRow(fieldName, fieldSchema, data[fieldName], `${bindPath}.${fieldName}`, data);
    if (group === "basic") {
      basicFields.push(row);
    } else {
      advancedFields.push(row);
    }
  }

  for (const row of basicFields) {
    container.appendChild(row);
  }

  if (advancedFields.length > 0) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "advanced-toggle";
    toggle.textContent = "Show advanced settings";
    container.appendChild(toggle);

    const advDiv = document.createElement("div");
    advDiv.className = "advanced-fields hidden";
    for (const row of advancedFields) {
      advDiv.appendChild(row);
    }
    container.appendChild(advDiv);

    toggle.addEventListener("click", () => {
      const hidden = advDiv.classList.toggle("hidden");
      toggle.textContent = hidden ? "Show advanced settings" : "Hide advanced settings";
    });
  }

  return container;
}

/**
 * Build a single field row (<div class="field-row">) for a schema property.
 *
 * @param {string} fieldName
 * @param {Object} fieldSchema  JSON Schema property object.
 * @param {*}      value        Current value.
 * @param {string} bindPath     Dot-path for the input name attribute.
 * @param {Object} parentData   Parent data dict (for context).
 * @returns {HTMLElement}
 */
function buildFieldRow(fieldName, fieldSchema, value, bindPath, parentData) {
  const row = document.createElement("div");
  row.className = "field-row";

  const label = document.createElement("label");
  label.textContent = fieldSchema.ui_label || fieldName;
  label.setAttribute("for", `field-${bindPath}`);
  row.appendChild(label);

  // Determine the placeholder text to show in empty inputs.
  // Two sources, checked in priority order:
  //   1. fieldSchema.ui_placeholder — explicit declaration in json_schema_extra,
  //      used for fields whose runtime default is derived (e.g. MQTT topic patterns).
  //   2. fieldSchema.default — the JSON Schema default value, for fields with a
  //      literal scalar default (e.g. port: 1883, topic_prefix: "mimir").
  let placeholderText = null;
  if (fieldSchema.ui_placeholder !== undefined) {
    placeholderText = String(fieldSchema.ui_placeholder);
  } else if (fieldSchema.default !== undefined && fieldSchema.default !== null) {
    placeholderText = String(fieldSchema.default);
  }

  // Resolve $ref if necessary.
  let resolvedSchema = fieldSchema;
  const ref = fieldSchema.$ref;
  if (ref) {
    resolvedSchema = resolveRef(ref) || fieldSchema;
  }

  // anyOf handling: pick the non-null variant if present.
  let effectiveSchema = resolvedSchema;
  if (resolvedSchema.anyOf) {
    const nonNull = resolvedSchema.anyOf.find(s => s.type !== "null" && !s.$ref?.includes("null"));
    if (nonNull) effectiveSchema = nonNull;
  }
  // If the selected variant is itself a $ref (e.g. anyOf: [$ref, null]),
  // resolve it so that type-checking branches below work correctly.
  if (effectiveSchema.$ref) {
    effectiveSchema = resolveRef(effectiveSchema.$ref) || effectiveSchema;
  }

  const type = effectiveSchema.type;
  const isNullable = fieldSchema.anyOf?.some(s => s.type === "null") ||
                     resolvedSchema.anyOf?.some(s => s.type === "null");

  let input;
  let uiSourceRendered = false;

  // ui_source: when the schema declares which gConfig section provides valid
  // device names for this field, render a <select> populated from those keys.
  // Falls back to a plain text input when gConfig has no entries in that
  // section (e.g. the user has not yet configured any pv_arrays).
  if (fieldSchema.ui_source && (effectiveSchema.type === "string" || isNullable)) {
    const deviceMap = (gConfig && gConfig[fieldSchema.ui_source]) || {};
    const deviceKeys = Object.keys(deviceMap);
    if (deviceKeys.length > 0) {
      const sel = document.createElement("select");
      sel.name = bindPath;
      sel.id = `field-${bindPath}`;

      // For nullable topic fields: an "(auto-derived)" first option that
      // clears the field so the runtime validator fills it in.
      if (isNullable) {
        const emptyOpt = document.createElement("option");
        emptyOpt.value = "";
        emptyOpt.textContent = "(auto-derived)";
        if (value === null || value === undefined || value === "") emptyOpt.selected = true;
        sel.appendChild(emptyOpt);
      }

      for (const key of deviceKeys) {
        const opt = document.createElement("option");
        if (fieldSchema.ui_placeholder) {
          // Topic-type field: the option value is the resolved full topic
          // string; the label shows the device name for readability.
          const resolved = resolvePlaceholder(fieldSchema.ui_placeholder, { deviceName: key });
          opt.value = resolved || key;
          opt.textContent = resolved ? `${key}  (${resolved})` : key;
          if (opt.value === value) opt.selected = true;
        } else {
          // Device-name field: the option value is the device key itself.
          opt.value = key;
          opt.textContent = key;
          if (key === value) opt.selected = true;
        }
        sel.appendChild(opt);
      }

      // Power-user escape hatch: selecting this option reveals a text input.
      const customOpt = document.createElement("option");
      customOpt.value = "__custom__";
      customOpt.textContent = "Custom value\u2026";
      // Select "Custom" when the current value matches none of the options.
      const knownValues = Array.from(sel.options).map(o => o.value);
      if (value !== null && value !== undefined && value !== "" && !knownValues.includes(String(value))) {
        customOpt.selected = true;
      }
      sel.appendChild(customOpt);

      // Text input shown only when "Custom value" is selected.
      const customInput = document.createElement("input");
      customInput.type = "text";
      // Same name so collectFormData picks up the typed value when visible.
      customInput.name = bindPath;
      customInput.style.cssText = "margin-top:0.3rem;display:none;width:100%;box-sizing:border-box;";
      customInput.placeholder = placeholderText || "";
      if (customOpt.selected) {
        customInput.value = value || "";
        customInput.style.display = "";
      }

      sel.addEventListener("change", () => {
        if (sel.value === "__custom__") {
          customInput.style.display = "";
          customInput.focus();
        } else {
          customInput.style.display = "none";
          customInput.value = "";
        }
      });

      row.appendChild(sel);
      row.appendChild(customInput);
      uiSourceRendered = true;
    }
  }

  if (!uiSourceRendered) {
  if (type === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value === true;
    input.name = bindPath;
    input.id = `field-${bindPath}`;
  } else if (type === "integer" || type === "number") {
    input = document.createElement("input");
    input.type = "number";
    input.name = bindPath;
    input.id = `field-${bindPath}`;
    if (value !== undefined && value !== null) input.value = value;
    if (effectiveSchema.minimum !== undefined) input.min = effectiveSchema.minimum;
    if (effectiveSchema.maximum !== undefined) input.max = effectiveSchema.maximum;
    if (effectiveSchema.exclusiveMinimum !== undefined) input.min = effectiveSchema.exclusiveMinimum;
    if (type === "number") input.step = "any";
    if (placeholderText) input.placeholder = placeholderText;
  } else if (effectiveSchema.enum) {
    input = document.createElement("select");
    input.name = bindPath;
    input.id = `field-${bindPath}`;
    if (isNullable) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = placeholderText
        ? `(default: ${placeholderText})`
        : "(not set)";
      input.appendChild(opt);
    }
    for (const opt of effectiveSchema.enum) {
      const el = document.createElement("option");
      el.value = opt;
      el.textContent = opt;
      if (opt === value) el.selected = true;
      input.appendChild(el);
    }
  } else if (type === "array" && effectiveSchema.items) {
    // Array of objects: render a sub-list.
    input = buildSubList(effectiveSchema, value || [], bindPath);
  } else if (type === "object" && effectiveSchema.additionalProperties && !effectiveSchema.properties) {
    // dict[str, Model]: JSON Schema shape is { type: "object", additionalProperties: { $ref: "..." } }.
    // The additionalProperties must reference a named $def (a model), not a primitive.
    const ap = effectiveSchema.additionalProperties;
    const apRef = ap?.$ref || (ap?.anyOf || []).find(s => s.$ref)?.$ref;
    if (apRef) {
      const defName = apRef.slice("#/$defs/".length);
      input = buildDictOfModel(defName, value || {}, bindPath, { ...(gSchema.$defs || {}) });
    } else {
      // dict[str, primitive] — fall through to a plain text input.
      input = document.createElement("input");
      input.type = "text";
      input.name = bindPath;
      input.id = `field-${bindPath}`;
      input.value = value !== undefined && value !== null ? JSON.stringify(value) : "";
    }
  } else if (type === "object" && effectiveSchema.properties) {
    // Nested object: recurse.
    const defRef = fieldSchema.$ref || (fieldSchema.anyOf || []).find(s => s.$ref)?.["$ref"];
    const nestedDef = defRef ? defRef.slice("#/$defs/".length) : null;
    if (nestedDef) {
      input = buildForm(nestedDef, value || {}, bindPath);
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.name = bindPath;
      input.id = `field-${bindPath}`;
      input.value = value !== undefined && value !== null ? JSON.stringify(value) : "";
    }
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.name = bindPath;
    input.id = `field-${bindPath}`;
    if (value !== undefined && value !== null) input.value = value;
    if (isNullable && (value === undefined || value === null)) input.value = "";
    if (placeholderText) input.placeholder = placeholderText;
  }

  row.appendChild(input);
  } // end if (!uiSourceRendered)

  if (fieldSchema.ui_unit) {
    const unit = document.createElement("span");
    unit.className = "field-unit";
    unit.textContent = fieldSchema.ui_unit;
    row.appendChild(unit);
  }

  // Phase 1 — derived topic hint.
  // Only for plain text inputs (not ui_source selects, which already show
  // the resolved value as option labels).
  const isEmpty = value === undefined || value === null || value === "";
  if (
    !uiSourceRendered &&
    fieldSchema.ui_placeholder !== undefined &&
    (effectiveSchema.type === "string" || isNullable) &&
    !effectiveSchema.enum &&
    typeof input.addEventListener === "function" &&
    input.tagName === "INPUT"
  ) {
    // Extract deviceName context from bindPath: last non-empty segment that
    // is not a known property name. For helper forms the bindPath is the
    // plain fieldName; for per-device forms it is "section.deviceName.field".
    // We pass ctx as null here; per-device callers that know the deviceName
    // can set input.dataset.deviceName before the hint is evaluated.
    const hintEl = document.createElement("small");
    hintEl.className = "field-hint topic-derived-hint";

    function updateHint() {
      const currentVal = input.value.trim();
      if (currentVal) {
        hintEl.textContent = "";
        hintEl.hidden = true;
        return;
      }
      const deviceName = input.dataset.deviceName || null;
      const resolved = resolvePlaceholder(fieldSchema.ui_placeholder, { deviceName });
      if (resolved) {
        hintEl.textContent = `Auto-derived: ${resolved}`;
        hintEl.hidden = false;
      } else {
        hintEl.textContent = "";
        hintEl.hidden = true;
      }
    }

    updateHint();
    input.addEventListener("input", updateHint);
    // Re-evaluate when the global prefix changes (Phase 2 fires this event).
    window.addEventListener("mimirPrefixChanged", updateHint);
    row.appendChild(hintEl);
  }

  if (fieldSchema.description) {
    const hint = document.createElement("small");
    hint.className = "field-hint";
    hint.textContent = fieldSchema.description;
    row.appendChild(hint);
  }

  return row;
}

/**
 * Build an editable dict-of-model widget for JSON Schema fields with shape:
 *   { type: "object", additionalProperties: { $ref: "#/$defs/SomeModel" } }
 *
 * This matches the project-standard pattern for named device collections,
 * e.g. `arrays: dict[str, ArrayConfig]`. Each entry is rendered as an
 * editable key input (the device name) plus a `buildForm` sub-form for the
 * model fields. Entries can be added and removed individually.
 *
 * The widget maintains a live `_liveData` object on the container element.
 * `collectFormData` reads this directly, so no bracket-path parsing is needed.
 *
 * @param {string} defName      The $defs name of the value model (e.g. "ArrayConfig").
 * @param {Object} data         Current dict value ({ key: modelObject, ... }).
 * @param {string} bindPath     Dot-path used by collectFormData to locate this field.
 * @param {Object} [defs]       Snapshot of $defs to use for lookups. Captured at
 *                              construction time so that deferred renders (add/remove
 *                              button handlers) still have access to the helper schema
 *                              definitions after buildHelperForm has restored gSchema.$defs.
 * @returns {HTMLElement}
 */
function buildDictOfModel(defName, data, bindPath, defs) {
  // Capture the current $defs at construction time so that event handlers
  // that fire after buildHelperForm restores gSchema.$defs can still resolve
  // definitions from the helper schema.
  const capturedDefs = defs || { ...(gSchema.$defs || {}) };
  const container = document.createElement("div");
  container.className = "dict-of-model-container";
  container.dataset.bindPath = bindPath;

  // liveData is the authoritative runtime state. Keys are entry names; values
  // are plain objects matching the model schema.
  const liveData = data ? { ...data } : {};
  Object.defineProperty(container, "_liveData", { get: () => ({ ...liveData }) });

  function renderEntries() {
    container.innerHTML = "";

    for (const [entryKey, entryData] of Object.entries(liveData)) {
      const entryDiv = document.createElement("div");
      entryDiv.className = "dict-entry";

      // Key row: label + editable name input.
      const keyRow = document.createElement("div");
      keyRow.className = "dict-entry-key-row";

      const keyLabel = document.createElement("label");
      keyLabel.textContent = "Name";
      keyRow.appendChild(keyLabel);

      const keyInput = document.createElement("input");
      keyInput.type = "text";
      keyInput.className = "dict-entry-key-input";
      keyInput.value = entryKey;
      keyInput.placeholder = "device name";
      // Rename on blur (not on input) to avoid re-renders while the user is typing.
      keyInput.addEventListener("blur", () => {
        const newKey = keyInput.value.trim();
        if (!newKey || newKey === entryKey) return;
        if (Object.prototype.hasOwnProperty.call(liveData, newKey)) {
          keyInput.value = entryKey;  // revert — duplicate key
          return;
        }
        // Preserve insertion order: rebuild liveData with the renamed key.
        const entries = Object.entries(liveData);
        const idx = entries.findIndex(([k]) => k === entryKey);
        entries[idx] = [newKey, entries[idx][1]];
        for (const k of Object.keys(liveData)) delete liveData[k];
        for (const [k, v] of entries) liveData[k] = v;
        renderEntries();
        container.dispatchEvent(new Event("change", { bubbles: true }));
      });
      keyRow.appendChild(keyInput);

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "remove-item-btn";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => {
        delete liveData[entryKey];
        renderEntries();
        container.dispatchEvent(new Event("change", { bubbles: true }));
      });
      keyRow.appendChild(removeBtn);
      entryDiv.appendChild(keyRow);

      // Sub-form for the model fields. Bind path uses the entry key so that
      // placeholder resolution (e.g. {array_key}) can look up the device name.
      // Temporarily install the captured defs so getDef() resolves correctly.
      const preBuildDefs = gSchema.$defs;
      gSchema.$defs = capturedDefs;
      const subForm = buildForm(defName, entryData || {}, `${bindPath}.${entryKey}`);
      gSchema.$defs = preBuildDefs;
      // Tag inputs in the sub-form with the device name for topic hint resolution.
      for (const inp of subForm.querySelectorAll("input[type='text']")) {
        inp.dataset.deviceName = entryKey;
      }
      // Keep liveData in sync as the user edits sub-form fields.
      subForm.addEventListener("change", () => {
        const collected = collectFormData(subForm);
        // collectFormData returns an object keyed by path segments. For a
        // sub-form built with bindPath "arrays.main", the result will have
        // nested keys. We extract the model object from the deepest level.
        liveData[entryKey] = _extractSubFormData(collected, bindPath, entryKey);
        container.dispatchEvent(new Event("change", { bubbles: true }));
      });

      entryDiv.appendChild(subForm);
      container.appendChild(entryDiv);
    }

    // Add-entry button.
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "add-item-btn";
    addBtn.textContent = "+ Add entry";
    addBtn.addEventListener("click", () => {
      // Generate a unique placeholder key.
      let newKey = "new_entry";
      let suffix = 1;
      while (Object.prototype.hasOwnProperty.call(liveData, newKey)) {
        newKey = `new_entry_${suffix++}`;
      }
      liveData[newKey] = {};
      renderEntries();
      container.dispatchEvent(new Event("change", { bubbles: true }));
    });
    container.appendChild(addBtn);
  }

  renderEntries();
  return container;
}

/**
 * Extract the model-level object from a collectFormData result for a dict-of-model entry.
 *
 * collectFormData returns a flat-ish structure keyed by the full dot-path. For
 * a sub-form at bindPath "arrays.main", the result might look like:
 *   { "arrays.main.peak_power_kwp": 5.2, "arrays.main.output_topic": "..." }
 * This helper traverses those keys to produce { peak_power_kwp: 5.2, ... }.
 *
 * @param {Object} collected  Output of collectFormData.
 * @param {string} bindPath   The dict field bind path (e.g. "arrays").
 * @param {string} entryKey   The entry key (e.g. "main").
 * @returns {Object}
 */
function _extractSubFormData(collected, bindPath, entryKey) {
  const prefix = `${bindPath}.${entryKey}.`;
  const result = {};
  for (const [k, v] of Object.entries(collected)) {
    if (k.startsWith(prefix)) {
      result[k.slice(prefix.length)] = v;
    } else if (typeof v === "object" && v !== null) {
      // collectFormData may nest objects; do a shallow merge if the key
      // matches the entry structure.
      Object.assign(result, v);
    }
  }
  return result;
}

/**
 * Build a sub-list for array fields.
 *
 * Handles three item shapes:
 *   1. Primitives (number, integer, string) — renders a single input per item.
 *   2. Objects with additionalProperties (e.g. scheduler schedule entries
 *      which are single-key dicts: { cronExpr: mqttTopic }) — renders a
 *      key + value pair of text inputs per item.
 *   3. Objects with named properties — renders one input per property.
 *
 * The component maintains a live JS array (_liveData) on the container
 * element. collectFormData reads this directly instead of parsing named
 * inputs, which avoids bracket-notation path-parsing issues.
 *
 * @param {Object}   arraySchema  JSON Schema for the array.
 * @param {Array}    items        Current array contents.
 * @param {string}   bindPath     Dot-path prefix used by collectFormData.
 * @returns {HTMLElement}
 */
function buildSubList(arraySchema, items, bindPath) {
  const container = document.createElement("div");
  container.className = "sublist-container";
  container.dataset.bindPath = bindPath;

  const itemSchema = arraySchema.items || {};
  const resolvedItemSchema = itemSchema.$ref ? (resolveRef(itemSchema.$ref) || itemSchema) : itemSchema;
  const isPrimitive = ["string", "number", "integer", "boolean"].includes(resolvedItemSchema.type);
  const isAdditionalProps = !isPrimitive
    && resolvedItemSchema.additionalProperties
    && !resolvedItemSchema.properties;

  // Live array — mutated in place. collectFormData reads container._liveData.
  const liveItems = items ? [...items] : [];
  container._liveData = liveItems;

  function renderItems() {
    container.innerHTML = "";

    liveItems.forEach((item, idx) => {
      const row = document.createElement("div");
      row.className = "sublist-item";

      if (isPrimitive) {
        const inp = document.createElement("input");
        inp.type = (resolvedItemSchema.type === "number" || resolvedItemSchema.type === "integer")
          ? "number" : "text";
        if (inp.type === "number") inp.step = "any";
        inp.value = item !== null && item !== undefined ? item : "";
        inp.style.flex = "1";
        inp.addEventListener("input", () => {
          liveItems[idx] = inp.type === "number"
            ? (inp.value === "" ? 0 : Number(inp.value))
            : inp.value;
          container.dispatchEvent(new Event("change", { bubbles: true }));
        });
        row.appendChild(inp);

      } else if (isAdditionalProps) {
        // Single-key dict, e.g. { "30 13 * * *": "nordpool/trigger" }.
        const entries = typeof item === "object" && item !== null ? Object.entries(item) : [];
        const [key, val] = entries.length > 0 ? entries[0] : ["", ""];

        const keyWrap = document.createElement("div");
        keyWrap.className = "sublist-field";
        const keyLbl = document.createElement("label");
        keyLbl.textContent = "Cron expression";
        const keyInp = document.createElement("input");
        keyInp.type = "text";
        keyInp.placeholder = "30 13 * * *";
        keyInp.value = key;
        keyWrap.appendChild(keyLbl);
        keyWrap.appendChild(keyInp);

        const valWrap = document.createElement("div");
        valWrap.className = "sublist-field";
        const valLbl = document.createElement("label");
        valLbl.textContent = "MQTT topic";
        const valInp = document.createElement("input");
        valInp.type = "text";
        valInp.placeholder = "e.g. nordpool/trigger";
        valInp.value = val !== null && val !== undefined ? val : "";
        valWrap.appendChild(valLbl);
        valWrap.appendChild(valInp);

        const update = () => {
          const k = keyInp.value;
          liveItems[idx] = k ? { [k]: valInp.value } : {};
          container.dispatchEvent(new Event("change", { bubbles: true }));
        };
        keyInp.addEventListener("input", update);
        valInp.addEventListener("input", update);

        row.appendChild(keyWrap);
        row.appendChild(valWrap);

      } else {
        // Array of objects with named properties.
        const props = resolvedItemSchema.properties || {};
        for (const [propName, propSchema] of Object.entries(props)) {
          const wrap = document.createElement("div");
          wrap.className = "sublist-field";
          const lbl = document.createElement("label");
          lbl.textContent = propSchema.ui_label || propName;
          const inp = document.createElement("input");
          inp.type = (propSchema.type === "integer" || propSchema.type === "number") ? "number" : "text";
          if (inp.type === "number") inp.step = "any";
          inp.value = item[propName] !== undefined && item[propName] !== null ? item[propName] : "";
          inp.addEventListener("input", () => {
            if (!liveItems[idx] || typeof liveItems[idx] !== "object") liveItems[idx] = {};
            liveItems[idx][propName] = inp.type === "number" ? Number(inp.value) : inp.value;
            container.dispatchEvent(new Event("change", { bubbles: true }));
          });
          wrap.appendChild(lbl);
          wrap.appendChild(inp);
          row.appendChild(wrap);
        }
      }

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "remove-subitem-btn";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => {
        liveItems.splice(idx, 1);
        renderItems();
        container.dispatchEvent(new Event("change", { bubbles: true }));
      });
      row.appendChild(removeBtn);
      container.appendChild(row);
    });

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "add-subitem-btn";
    addBtn.textContent = "+ Add item";
    addBtn.addEventListener("click", () => {
      liveItems.push(
        isPrimitive
          ? (resolvedItemSchema.type === "number" || resolvedItemSchema.type === "integer" ? 0 : "")
          : {}
      );
      renderItems();
      container.dispatchEvent(new Event("change", { bubbles: true }));
    });
    container.appendChild(addBtn);
  }

  renderItems();
  return container;
}

// ---------------------------------------------------------------------------
// Form data collector
// ---------------------------------------------------------------------------

/**
 * Collect form field values from a container element by reading named inputs.
 *
 * Returns a nested dict matching the bindPath structure. Values are coerced
 * to the correct types based on the input element type.
 *
 * @param {HTMLElement} formEl
 * @returns {Object}
 */
function collectFormData(formEl) {
  const result = {};
  const inputs = formEl.querySelectorAll("input, select, textarea");
  for (const input of inputs) {
    // Sublist inputs have no name attribute and are managed via _liveData.
    if (!input.name) continue;
    let value;
    if (input.type === "checkbox") {
      value = input.checked;
    } else if (input.type === "number") {
      value = input.value === "" ? null : Number(input.value);
    } else {
      value = input.value === "" ? null : input.value;
    }
    setNestedValue(result, input.name.split("."), value);
  }
  // Collect array fields from sublist containers via their live arrays.
  // An empty array is treated as null so that nullable list fields with
  // min_length constraints (e.g. charge_efficiency_curve) are not sent as []
  // when the user has not added any items. Pydantic then applies the field
  // default (None) rather than rejecting an empty list.
  for (const sl of formEl.querySelectorAll(".sublist-container")) {
    const path = sl.dataset.bindPath;
    if (path && sl._liveData !== undefined) {
      setNestedValue(result, path.split("."), sl._liveData.length === 0 ? null : sl._liveData);
    }
  }
  return result;
}

/**
 * Recursively remove null values from an object.
 *
 * Null values in the payload correspond to form fields the user left empty.
 * Omitting them lets Pydantic apply field defaults (e.g. 0.0 for
 * optional numeric fields) rather than rejecting null as an invalid type.
 * Array items are preserved as-is; only object keys are pruned.
 *
 * @param {*} obj
 * @returns {*}
 */
function stripNulls(obj) {
  if (Array.isArray(obj)) {
    return obj.map(item => (item !== null && typeof item === "object") ? stripNulls(item) : item);
  }
  if (obj !== null && typeof obj === "object") {
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      if (v === null) continue;
      out[k] = (typeof v === "object") ? stripNulls(v) : v;
    }
    return out;
  }
  return obj;
}

/**
 * Set a value in a nested object at a dot-path.
 * @param {Object} obj
 * @param {string[]} keys
 * @param {*} value
 */
function setNestedValue(obj, keys, value) {
  let cur = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    if (!(k in cur)) cur[k] = {};
    cur = cur[k];
  }
  const lastKey = keys[keys.length - 1];
  cur[lastKey] = value;
}

// ---------------------------------------------------------------------------
// DeviceListEditor — generic CRUD component for named-map device sections
// ---------------------------------------------------------------------------

/**
 * A generic CRUD component that manages a named-map device section.
 *
 * The component is driven entirely by the schema definition named by
 * schemaRef. Adding a new device type requires only a new instantiation
 * of this class — no modifications to DeviceListEditor itself.
 *
 * @param {Object} options
 * @param {string} options.sectionKey          Top-level key in the config dict.
 * @param {string} options.schemaRef           $defs key for the device schema.
 * @param {string} options.tabLabel            Tab label (unused here, passed in).
 * @param {string} options.newInstanceNamePlaceholder  Placeholder for the name input.
 */
class DeviceListEditor {
  constructor({ sectionKey, schemaRef, tabLabel, newInstanceNamePlaceholder }) {
    this.sectionKey = sectionKey;
    this.schemaRef = schemaRef;
    this.tabLabel = tabLabel;
    this.newInstanceNamePlaceholder = newInstanceNamePlaceholder || "e.g. my_device";
    this._selectedName = null;
  }

  /**
   * Render the full CRUD panel into a container element.
   * Called by the tab system when this tab is activated.
   *
   * @param {HTMLElement} container
   */
  render(container) {
    container.innerHTML = "";

    const section = gConfig[this.sectionKey] || {};
    const names = Object.keys(section);

    const crud = document.createElement("div");
    crud.className = "crud-container";

    // Left panel: instance list + add row.
    const listPanel = document.createElement("div");
    listPanel.className = "crud-list-panel";

    const ul = document.createElement("ul");
    ul.className = "crud-instance-list";

    const detailPanel = document.createElement("div");
    detailPanel.className = "crud-detail-panel";

    const renderDetail = (name) => {
      detailPanel.innerHTML = "";
      if (!name) {
        const hint = document.createElement("p");
        hint.className = "crud-empty-hint";
        hint.textContent = names.length === 0
          ? `No ${this.sectionKey} configured yet. Add one using the panel on the left.`
          : "Select an instance from the list to edit it.";
        detailPanel.appendChild(hint);
        return;
      }

      const instanceData = section[name] || {};
      const titleEl = document.createElement("h2");
      titleEl.textContent = name;
      titleEl.style.marginTop = "0";
      detailPanel.appendChild(titleEl);

      const formEl = buildForm(this.schemaRef, instanceData, `${this.sectionKey}.${name}`);
      detailPanel.appendChild(formEl);

      // Wire changes back to gConfig.
      formEl.addEventListener("change", () => {
        const updated = collectFormData(formEl);
        const instancePath = `${this.sectionKey}.${name}`;
        // Update only the individual instance path in gConfig.
        if (!gConfig[this.sectionKey]) gConfig[this.sectionKey] = {};
        gConfig[this.sectionKey][name] = getNestedValue(updated, instancePath.split("."));
      });
    };

    if (names.length === 0) {
      const empty = document.createElement("li");
      empty.style.padding = "0.5rem 0.75rem";
      empty.style.color = "var(--color-muted)";
      empty.style.fontSize = "0.85rem";
      empty.textContent = "None configured.";
      ul.appendChild(empty);
    }

    for (const name of names) {
      const li = this._buildInstanceItem(name, ul, section, detailPanel, renderDetail);
      ul.appendChild(li);
    }

    listPanel.appendChild(ul);

    // Add row.
    const addRow = document.createElement("div");
    addRow.className = "crud-add-row";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = this.newInstanceNamePlaceholder;
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.textContent = `+ Add`;
    addBtn.addEventListener("click", () => {
      const name = nameInput.value.trim();
      if (!name) return;
      if (gConfig[this.sectionKey] && gConfig[this.sectionKey][name]) {
        alert(`An instance named '${name}' already exists.`);
        return;
      }
      if (!gConfig[this.sectionKey]) gConfig[this.sectionKey] = {};
      gConfig[this.sectionKey][name] = {};
      this._selectedName = name;
      // Re-render this tab.
      this.render(container);
    });
    addRow.appendChild(nameInput);
    addRow.appendChild(addBtn);
    listPanel.appendChild(addRow);

    crud.appendChild(listPanel);
    crud.appendChild(detailPanel);
    container.appendChild(crud);

    // Render detail for selected instance.
    renderDetail(this._selectedName && section[this._selectedName] !== undefined
      ? this._selectedName : (names[0] || null));
  }

  _buildInstanceItem(name, ul, section, detailPanel, renderDetail) {
    const li = document.createElement("li");
    li.className = "crud-instance-item";
    if (name === this._selectedName) li.classList.add("selected");

    const nameSpan = document.createElement("span");
    nameSpan.textContent = name;
    li.appendChild(nameSpan);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "remove-btn";
    removeBtn.textContent = "Remove";
    removeBtn.title = `Remove ${name}`;
    removeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!confirm(`Remove '${name}'?`)) return;
      delete gConfig[this.sectionKey][name];
      if (this._selectedName === name) this._selectedName = null;
      // Re-render the parent container by re-calling render.
      const container = ul.closest(".crud-container").parentElement;
      this.render(container);
    });
    li.appendChild(removeBtn);

    li.addEventListener("click", () => {
      ul.querySelectorAll(".crud-instance-item").forEach(el => el.classList.remove("selected"));
      li.classList.add("selected");
      this._selectedName = name;
      renderDetail(name);
    });

    return li;
  }
}

/**
 * Get a nested value from an object by dot-path.
 * @param {Object} obj
 * @param {string[]} keys
 * @returns {*}
 */
function getNestedValue(obj, keys) {
  let cur = obj;
  for (const k of keys) {
    if (cur == null) return undefined;
    cur = cur[k];
  }
  return cur;
}

// ---------------------------------------------------------------------------
// Tab system
// ---------------------------------------------------------------------------

/**
 * Register a tab with a label and a render function.
 *
 * @param {string}   label   Display label.
 * @param {function} renderFn  Called with (container: HTMLElement) when active.
 */
function registerTab(label, renderFn) {
  gTabs.push({ label, renderFn });
}

/**
 * Register a group of tabs that appear as a single dropdown button in the bar.
 *
 * @param {string} label     The dropdown button label.
 * @param {Array}  tabs      Array of {label, renderFn} objects.
 */
function registerGroup(label, tabs) {
  gTabs.push({ label, group: true, tabs });
}

/**
 * Find a tab entry by label, searching both flat tabs and groups.
 *
 * @param {string} label
 * @returns {{label: string, renderFn: function}|null}
 */
function _findTabEntry(label) {
  for (const entry of gTabs) {
    if (entry.group) {
      const inner = entry.tabs.find(t => t.label === label);
      if (inner) return inner;
    } else if (entry.label === label) {
      return entry;
    }
  }
  return null;
}

function buildTabBar() {
  const bar = document.getElementById("tab-bar");
  bar.innerHTML = "";
  const activeHash = location.hash.slice(1) || gTabs[0]?.label;

  for (const entry of gTabs) {
    if (entry.group) {
      // Render as a dropdown button with a flyout menu.
      const isGroupActive = entry.tabs.some(t => t.label === activeHash);
      const wrapper = document.createElement("div");
      wrapper.className = "tab-group";

      const groupBtn = document.createElement("button");
      groupBtn.type = "button";
      groupBtn.className = "tab-group-btn" + (isGroupActive ? " active" : "");
      groupBtn.innerHTML = entry.label + " <span class='tab-group-caret'>&#x25BE;</span>";

      const menu = document.createElement("ul");
      menu.className = "tab-group-menu";

      for (const sub of entry.tabs) {
        const li = document.createElement("li");
        li.className = "tab-group-item" + (sub.label === activeHash ? " active" : "");
        li.textContent = sub.label;
        li.addEventListener("click", () => {
          menu.classList.remove("open");
          location.hash = sub.label;
          activateTab(sub.label);
        });
        menu.appendChild(li);
      }

      groupBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        // Close any other open menus first.
        bar.querySelectorAll(".tab-group-menu.open").forEach(m => {
          if (m !== menu) m.classList.remove("open");
        });
        menu.classList.toggle("open");
      });

      wrapper.appendChild(groupBtn);
      wrapper.appendChild(menu);
      bar.appendChild(wrapper);
    } else {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab-btn" + (entry.label === activeHash ? " active" : "");
      btn.textContent = entry.label;
      btn.addEventListener("click", () => {
        location.hash = entry.label;
        activateTab(entry.label);
      });
      bar.appendChild(btn);
    }
  }

  // Close open menus when clicking anywhere outside the tab bar.
  document.addEventListener("click", () => {
    bar.querySelectorAll(".tab-group-menu.open").forEach(m => m.classList.remove("open"));
  });
}

function activateTab(label) {
  const content = document.getElementById("content-pane");
  const bar = document.getElementById("tab-bar");

  // Update flat tab buttons.
  bar.querySelectorAll(".tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.textContent === label);
  });

  // Update group buttons and their menu items.
  bar.querySelectorAll(".tab-group").forEach(wrapper => {
    const menu = wrapper.querySelector(".tab-group-menu");
    const groupBtn = wrapper.querySelector(".tab-group-btn");
    let groupHasActive = false;
    menu.querySelectorAll(".tab-group-item").forEach(li => {
      const isActive = li.textContent === label;
      li.classList.toggle("active", isActive);
      if (isActive) groupHasActive = true;
    });
    groupBtn.classList.toggle("active", groupHasActive);
    menu.classList.remove("open");
  });

  const tab = _findTabEntry(label);
  if (!tab) return;
  content.innerHTML = "";
  tab.renderFn(content);
}

// ---------------------------------------------------------------------------
// General tab renderer
// ---------------------------------------------------------------------------

/**
 * Render the "General" tab, which shows top-level simple fields (MQTT, grid)
 * from MimirheimConfig.
 *
 * @param {HTMLElement} container
 */
function renderGeneralTab(container) {
  container.innerHTML = "";

  // Render the mqtt sub-section.
  const mqttSection = document.createElement("div");
  mqttSection.className = "form-section";
  const mqttH = document.createElement("h2");
  mqttH.textContent = "MQTT";
  mqttSection.appendChild(mqttH);

  const hasMqttEnv = gMqttEnv && Object.keys(gMqttEnv).length > 0;

  if (hasMqttEnv) {
    // Supervisor provides MQTT settings — hide the form by default and expose
    // an opt-in override for users who need different broker settings per
    // instance (e.g. multiple add-on installs pointing at different brokers).
    const banner = document.createElement("div");
    banner.className = "info-banner";
    banner.textContent = "MQTT connection settings are provided by the HA Supervisor "
      + "and do not need to be configured here. "
      + "Enable the override below only if this instance requires different broker settings.";
    mqttSection.appendChild(banner);

    // Auto-enable override when the saved YAML already has an explicit mqtt section.
    if (gConfig.mqtt && Object.keys(gConfig.mqtt).length > 0) {
      gMqttOverride = true;
    }

    const overrideLabel = document.createElement("label");
    overrideLabel.style.cssText = "display:flex;align-items:center;gap:0.5rem;margin-bottom:1rem;cursor:pointer;font-weight:500;";
    const overrideCheck = document.createElement("input");
    overrideCheck.type = "checkbox";
    overrideCheck.checked = gMqttOverride;
    overrideLabel.appendChild(overrideCheck);
    overrideLabel.appendChild(document.createTextNode("Override MQTT settings for this instance"));
    mqttSection.appendChild(overrideLabel);

    const mqttFormArea = document.createElement("div");
    mqttSection.appendChild(mqttFormArea);

    // Phase 2 banner container — placed between MQTT section and rest of form.
    const prefixSyncBanner = document.createElement("div");
    mqttSection.appendChild(prefixSyncBanner);

    function renderMqttForm() {
      mqttFormArea.innerHTML = "";
      if (!overrideCheck.checked) return;
      const merged = mergedWithMqttEnv(gConfig.mqtt ? { mqtt: gConfig.mqtt } : {});
      const mqttForm = buildForm("MqttConfig", merged.mqtt || {}, "mqtt");
      mqttFormArea.appendChild(mqttForm);
      mqttForm.addEventListener("change", () => {
        gConfig.mqtt = collectFormData(mqttForm).mqtt;
      });
      _wirePrefixSync(mqttForm, prefixSyncBanner);
    }

    overrideCheck.addEventListener("change", () => {
      gMqttOverride = overrideCheck.checked;
      if (!gMqttOverride) delete gConfig.mqtt;
      renderMqttForm();
    });

    renderMqttForm();
  } else {
    // No env vars — show MQTT form directly. mqtt is required for mimirheim
    // to function so it must always be present in the YAML in this mode.
    const prefixSyncBanner = document.createElement("div");
    const mqttForm = buildForm("MqttConfig", gConfig.mqtt || {}, "mqtt");
    mqttSection.appendChild(mqttForm);
    mqttSection.appendChild(prefixSyncBanner);
    mqttForm.addEventListener("change", () => {
      gConfig.mqtt = collectFormData(mqttForm).mqtt;
    });
    _wirePrefixSync(mqttForm, prefixSyncBanner);
  }

  container.appendChild(mqttSection);

  // Render the grid sub-section.
  const gridSection = document.createElement("div");
  gridSection.className = "form-section";
  const gridH = document.createElement("h2");
  gridH.textContent = "Grid";
  gridSection.appendChild(gridH);
  const gridForm = buildForm("GridConfig", gConfig.grid || {}, "grid");
  gridSection.appendChild(gridForm);
  gridForm.addEventListener("change", () => {
    gConfig.grid = collectFormData(gridForm).grid;
  });
  container.appendChild(gridSection);

  // All remaining scalar config sections, rendered with the same pattern.
  const scalarSections = [
    { key: "homeassistant", schemaRef: "HomeAssistantConfig", label: "Home Assistant discovery" },
    { key: "outputs",       schemaRef: "OutputsConfig",       label: "Output topics" },
    { key: "inputs",        schemaRef: "InputsConfig",        label: "Input topics" },
    { key: "reporting",     schemaRef: "ReportingConfig",     label: "Reporting" },
    { key: "debug",         schemaRef: "DebugConfig",         label: "Debug" },
    { key: "objectives",    schemaRef: "ObjectivesConfig",    label: "Objectives" },
    { key: "constraints",   schemaRef: "ConstraintsConfig",   label: "Constraints" },
    { key: "solver",        schemaRef: "SolverConfig",        label: "Solver" },
    { key: "readiness",     schemaRef: "ReadinessConfig",     label: "Readiness" },
    { key: "control",       schemaRef: "ControlConfig",       label: "Control" },
  ];
  for (const { key, schemaRef, label } of scalarSections) {
    const sec = document.createElement("div");
    sec.className = "form-section";
    const h = document.createElement("h2");
    h.textContent = label;
    sec.appendChild(h);
    const form = buildForm(schemaRef, gConfig[key] || {}, key);
    sec.appendChild(form);
    form.addEventListener("change", () => {
      gConfig[key] = collectFormData(form)[key];
    });
    container.appendChild(sec);
  }
}

// ---------------------------------------------------------------------------
// CRUD instances — one per named-map device type
// Adding a new device type: instantiate DeviceListEditor with sectionKey,
// schemaRef, tabLabel, and newInstanceNamePlaceholder. Register it in init().
// No changes to DeviceListEditor itself are required.
// ---------------------------------------------------------------------------

const BatteriesCrud = new DeviceListEditor({
  sectionKey: "batteries",
  schemaRef: "BatteryConfig",
  tabLabel: "Batteries",
  newInstanceNamePlaceholder: "e.g. home_battery",
});

const PvArraysCrud = new DeviceListEditor({
  sectionKey: "pv_arrays",
  schemaRef: "PvConfig",
  tabLabel: "PV Arrays",
  newInstanceNamePlaceholder: "e.g. roof_pv",
});

const EvChargersCrud = new DeviceListEditor({
  sectionKey: "ev_chargers",
  schemaRef: "EvConfig",
  tabLabel: "EV Chargers",
  newInstanceNamePlaceholder: "e.g. ev_charger",
});

const HybridInvertersCrud = new DeviceListEditor({
  sectionKey: "hybrid_inverters",
  schemaRef: "HybridInverterConfig",
  tabLabel: "Hybrid Inverters",
  newInstanceNamePlaceholder: "e.g. hybrid_inv",
});

const DeferrableLoadsCrud = new DeviceListEditor({
  sectionKey: "deferrable_loads",
  schemaRef: "DeferrableLoadConfig",
  tabLabel: "Deferrable Loads",
  newInstanceNamePlaceholder: "e.g. washing_machine",
});

const StaticLoadsCrud = new DeviceListEditor({
  sectionKey: "static_loads",
  schemaRef: "StaticLoadConfig",
  tabLabel: "Static Loads",
  newInstanceNamePlaceholder: "e.g. base_load",
});

const ThermalBoilersCrud = new DeviceListEditor({
  sectionKey: "thermal_boilers",
  schemaRef: "ThermalBoilerConfig",
  tabLabel: "Boilers",
  newInstanceNamePlaceholder: "e.g. hot_water",
});

const SpaceHeatingCrud = new DeviceListEditor({
  sectionKey: "space_heating",
  schemaRef: "SpaceHeatingConfig",
  tabLabel: "Space Heating",
  newInstanceNamePlaceholder: "e.g. space_hp",
});

const CombiHeatPumpsCrud = new DeviceListEditor({
  sectionKey: "combi_heat_pumps",
  schemaRef: "CombiHeatPumpConfig",
  tabLabel: "Combi Heat Pumps",
  newInstanceNamePlaceholder: "e.g. combi_hp",
});

// ---------------------------------------------------------------------------
// Heating sub-tab renderer
// Three heating device types in one tab with sub-tab navigation.
// ---------------------------------------------------------------------------

/**
 * Render the "Heating" tab with three sub-tabs: Boilers, Space Heating, Combi.
 * @param {HTMLElement} container
 */
function renderHeatingTab(container) {
  container.innerHTML = "";

  const subTabs = [
    { label: "Boilers", crud: ThermalBoilersCrud },
    { label: "Space Heating", crud: SpaceHeatingCrud },
    { label: "Combi Heat Pumps", crud: CombiHeatPumpsCrud },
  ];

  let activeSubTab = subTabs[0].label;

  const subBar = document.createElement("div");
  subBar.style.cssText = "display:flex;gap:0;border-bottom:1px solid var(--color-border);margin-bottom:1rem;";

  const subContent = document.createElement("div");

  function activateSub(label) {
    activeSubTab = label;
    subBar.querySelectorAll("button").forEach(b => {
      b.classList.toggle("active", b.textContent === label);
    });
    subContent.innerHTML = "";
    const tab = subTabs.find(t => t.label === label);
    if (tab) tab.crud.render(subContent);
  }

  for (const sub of subTabs) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tab-btn" + (sub.label === activeSubTab ? " active" : "");
    btn.textContent = sub.label;
    btn.addEventListener("click", () => activateSub(sub.label));
    subBar.appendChild(btn);
  }

  container.appendChild(subBar);
  container.appendChild(subContent);
  activateSub(activeSubTab);
}

// ---------------------------------------------------------------------------
// Helper tab renderer
// ---------------------------------------------------------------------------

/**
 * Global state for helper configs and schemas, loaded once in init().
 * @type {Object|null}
 */
let gHelperConfigs = null;
let gHelperSchemas = null;

/**
 * Map from helper config filename to its corresponding schema title.
 * Used to find the right schema in gHelperSchemas.
 */
const HELPER_FILE_TO_TITLE = {
  "nordpool.yaml":        "Nordpool",
  "pv-fetcher.yaml":      "PV Forecast (forecast.solar)",
  "pv-ml-learner.yaml":   "PV ML Learner",
  "reporter.yaml":        "Reporter",
  "scheduler.yaml":       "Scheduler",
};

/**
 * Baseload variant file names and their display labels.
 */
const BASELOAD_VARIANTS = [
  { file: "baseload-static.yaml", label: "Static profile" },
  { file: "baseload-ha.yaml",     label: "Home Assistant REST API" },
  { file: "baseload-ha-db.yaml",  label: "Home Assistant database" },
];

/**
 * Render a helper tab for a single config file (non-baseload).
 *
 * Shows an enable/disable toggle at the top. When enabled, shows the form
 * for the helper's config. The save button calls POST /api/helper-config/<file>.
 *
 * @param {HTMLElement} container
 * @param {string} filename  Config filename (e.g. "nordpool.yaml").
 */
function renderHelperTab(container, filename) {
  container.innerHTML = "";
  if (!gHelperConfigs || !gHelperSchemas) {
    container.innerHTML = "<p>Loading helper data…</p>";
    return;
  }

  const state = gHelperConfigs[filename] || { enabled: false, config: {} };
  const schema = gHelperSchemas[filename];

  // Enable/disable toggle.
  const toggleRow = document.createElement("div");
  toggleRow.style.cssText = "display:flex;align-items:center;gap:0.75rem;margin-bottom:1rem;";
  const enabledLabel = document.createElement("label");
  enabledLabel.style.fontWeight = "600";
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = state.enabled;
  toggle.style.marginRight = "0.4rem";
  enabledLabel.appendChild(toggle);
  enabledLabel.appendChild(document.createTextNode("Enable this helper"));
  toggleRow.appendChild(enabledLabel);

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.style.cssText = "padding:0.35rem 1rem;background:var(--color-primary);color:#fff;border:none;border-radius:4px;cursor:pointer;";
  saveBtn.textContent = "Save";
  toggleRow.appendChild(saveBtn);

  const statusSpan = document.createElement("span");
  statusSpan.style.cssText = "font-size:0.85rem;color:var(--color-muted);";
  toggleRow.appendChild(statusSpan);
  container.appendChild(toggleRow);

  // Form area (shown only when enabled).
  const formArea = document.createElement("div");
  container.appendChild(formArea);

  function renderForm() {
    formArea.innerHTML = "";
    if (!toggle.checked) {
      const hint = document.createElement("p");
      hint.style.color = "var(--color-muted)";
      hint.style.fontSize = "0.85rem";
      hint.textContent = "Toggle on to configure and enable this helper service.";
      formArea.appendChild(hint);
      return;
    }
    if (!schema) {
      formArea.innerHTML = "<p>Schema not available for this helper.</p>";
      return;
    }
    const formEl = buildHelperForm(schema, mergedWithMqttEnv(state.config || {}), filename);
    formArea.appendChild(formEl);
  }

  toggle.addEventListener("change", renderForm);
  renderForm();

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    statusSpan.textContent = "Saving…";
    let body;
    if (!toggle.checked) {
      body = { enabled: false };
    } else {
      const formEl = formArea.querySelector("form, div.helper-form");
      const cfg = formEl ? collectHelperFormData(formEl, schema) : {};
      stripMqttEnvFields(cfg);
      body = { enabled: true, config: cfg };
    }
    try {
      const resp = await fetch(`api/helper-config/${filename}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (data.ok) {
        statusSpan.textContent = "Saved. Container restart required for s6 service changes.";
        // Refresh state.
        const refreshed = await fetch("api/helper-configs");
        gHelperConfigs = await refreshed.json();
        setTimeout(() => { statusSpan.textContent = ""; }, 6000);
      } else {
        const errors = (data.errors || []).map(e =>
          typeof e === "object" ? `${(e.loc || []).join(".")}: ${e.msg}` : String(e)
        );
        statusSpan.textContent = "Errors: " + errors.join("; ");
      }
    } catch (err) {
      statusSpan.textContent = `Error: ${err.message}`;
    } finally {
      saveBtn.disabled = false;
    }
  });
}

/**
 * Render the Baseload tab with a variant selector.
 * Only one of the three baseload variants may be active at once.
 * @param {HTMLElement} container
 */
function renderBaseloadTab(container) {
  container.innerHTML = "";
  if (!gHelperConfigs || !gHelperSchemas) {
    container.innerHTML = "<p>Loading helper data…</p>";
    return;
  }

  // Determine which variant (if any) is currently enabled.
  const activeVariant = BASELOAD_VARIANTS.find(v => gHelperConfigs[v.file]?.enabled);

  const enabledLabel = document.createElement("label");
  enabledLabel.style.cssText = "display:flex;align-items:center;gap:0.5rem;margin-bottom:1rem;font-weight:600;";
  const enabledToggle = document.createElement("input");
  enabledToggle.type = "checkbox";
  enabledToggle.checked = !!activeVariant;
  enabledLabel.appendChild(enabledToggle);
  enabledLabel.appendChild(document.createTextNode("Enable baseload helper"));
  container.appendChild(enabledLabel);

  const variantRow = document.createElement("div");
  variantRow.style.cssText = "display:flex;align-items:center;gap:0.75rem;margin-bottom:1rem;";
  const variantLabel = document.createElement("label");
  variantLabel.textContent = "Variant: ";
  variantLabel.style.fontWeight = "500";
  const variantSelect = document.createElement("select");
  variantSelect.style.cssText = "padding:0.3rem 0.5rem;border:1px solid var(--color-border);border-radius:4px;";
  for (const v of BASELOAD_VARIANTS) {
    const opt = document.createElement("option");
    opt.value = v.file;
    opt.textContent = v.label;
    if (activeVariant && activeVariant.file === v.file) opt.selected = true;
    variantSelect.appendChild(opt);
  }
  variantRow.appendChild(variantLabel);
  variantRow.appendChild(variantSelect);
  container.appendChild(variantRow);

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.style.cssText = "padding:0.35rem 1rem;background:var(--color-primary);color:#fff;border:none;border-radius:4px;cursor:pointer;margin-bottom:1rem;";
  saveBtn.textContent = "Save";
  container.appendChild(saveBtn);

  const statusSpan = document.createElement("span");
  statusSpan.style.cssText = "font-size:0.85rem;color:var(--color-muted);margin-left:0.5rem;";
  container.appendChild(statusSpan);

  const formArea = document.createElement("div");
  container.appendChild(formArea);

  function renderVariantForm() {
    formArea.innerHTML = "";
    if (!enabledToggle.checked) return;
    const selectedFile = variantSelect.value;
    const schema = gHelperSchemas[selectedFile];
    const existingConfig = gHelperConfigs[selectedFile]?.config || {};
    if (schema) {
      formArea.appendChild(buildHelperForm(schema, mergedWithMqttEnv(existingConfig), selectedFile));
    }
  }

  enabledToggle.addEventListener("change", renderVariantForm);
  variantSelect.addEventListener("change", renderVariantForm);
  renderVariantForm();

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    statusSpan.textContent = "Saving…";
    const selectedFile = variantSelect.value;
    let body;
    if (!enabledToggle.checked) {
      // Disable whichever variant is active.
      const toDisable = BASELOAD_VARIANTS.filter(v => gHelperConfigs[v.file]?.enabled).map(v => v.file);
      for (const f of toDisable) {
        await fetch(`api/helper-config/${f}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: false }),
        });
      }
      statusSpan.textContent = "Disabled.";
      const refreshed = await fetch("api/helper-configs");
      gHelperConfigs = await refreshed.json();
      saveBtn.disabled = false;
      return;
    }
    const formEl = formArea.querySelector("div.helper-form");
    const cfg = formEl ? collectHelperFormData(formEl, gHelperSchemas[selectedFile]) : {};
    stripMqttEnvFields(cfg);
    body = { enabled: true, config: cfg };
    try {
      const resp = await fetch(`api/helper-config/${selectedFile}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (data.ok) {
        statusSpan.textContent = "Saved. Container restart required.";
        const refreshed = await fetch("api/helper-configs");
        gHelperConfigs = await refreshed.json();
        setTimeout(() => { statusSpan.textContent = ""; }, 6000);
      } else {
        const errors = (data.errors || []).map(e =>
          typeof e === "object" ? `${(e.loc || []).join(".")}: ${e.msg}` : String(e)
        );
        statusSpan.textContent = "Errors: " + errors.join("; ");
      }
    } catch (err) {
      statusSpan.textContent = `Error: ${err.message}`;
    } finally {
      saveBtn.disabled = false;
    }
  });
}

/**
 * Build a form element for a helper config using its schema.
 * Returns a div with class "helper-form".
 *
 * @param {Object} schema  The helper's JSON Schema object.
 * @param {Object} data    Current config dict.
 * @param {string} prefix  Filename used as form field name prefix.
 * @returns {HTMLElement}
 */
function buildHelperForm(schema, data, prefix) {
  const wrapper = document.createElement("div");
  wrapper.className = "helper-form";

  const props = schema.properties || {};
  const defs = schema.$defs || {};

  // Temporarily merge defs into gSchema.$defs for resolveRef to work.
  const savedDefs = gSchema.$defs || {};
  gSchema.$defs = { ...savedDefs, ...defs };

  const basicFields = [];
  const advancedFields = [];

  for (const [fieldName, fieldSchema] of Object.entries(props)) {
    const group = fieldSchema.ui_group || "advanced";
    // Use fieldName directly as the bind path so that collectFormData
    // produces a clean {fieldName: value} dict without any filename prefix.
    // Filenames like "nordpool.yaml" contain dots which would be split
    // incorrectly by collectFormData if included in the path.
    const row = buildFieldRow(fieldName, fieldSchema, data[fieldName], fieldName, data);
    if (group === "basic") basicFields.push(row);
    else advancedFields.push(row);
  }

  for (const row of basicFields) wrapper.appendChild(row);

  if (advancedFields.length > 0) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "advanced-toggle";
    toggle.textContent = "Show advanced settings";
    wrapper.appendChild(toggle);
    const advDiv = document.createElement("div");
    advDiv.className = "advanced-fields hidden";
    for (const row of advancedFields) advDiv.appendChild(row);
    wrapper.appendChild(advDiv);
    toggle.addEventListener("click", () => {
      const hidden = advDiv.classList.toggle("hidden");
      toggle.textContent = hidden ? "Show advanced settings" : "Hide advanced settings";
    });
  }

  gSchema.$defs = savedDefs;
  return wrapper;
}

/**
 * Collect form data from a helper form element.
 * Strips the filename prefix from input names.
 *
 * @param {HTMLElement} formEl
 * @param {Object} schema  The helper schema (for type coercion).
 * @returns {Object}
 */
function collectHelperFormData(formEl, schema) {
  // buildHelperForm uses plain field names (not prefixed with the filename),
  // so collectFormData returns the config dict directly.
  return collectFormData(formEl);
}

// ---------------------------------------------------------------------------
// Save logic
// ---------------------------------------------------------------------------

async function saveConfig() {
  const btn = document.getElementById("save-btn");
  const status = document.getElementById("save-status");
  btn.disabled = true;
  status.textContent = "Saving…";

  try {
    let payload = JSON.parse(JSON.stringify(gConfig));
    // Strip null values so that empty form fields cause Pydantic to apply field
    // defaults rather than failing type validation for non-nullable fields.
    payload = stripNulls(payload);
    // When MQTT is Supervisor-controlled and the override toggle is off, exclude
    // the mqtt section from the payload entirely. The server merges Supervisor
    // values for validation; the solver reads them from the Supervisor at
    // runtime so they must not be persisted in the YAML file.
    if (gMqttEnv && Object.keys(gMqttEnv).length > 0 && !gMqttOverride) {
      delete payload.mqtt;
    }
    const resp = await fetch("api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.ok) {
      status.textContent = "Saved.";
      setTimeout(() => { status.textContent = ""; }, 3000);
    } else {
      const errors = (data.errors || []).map(e => `${e.loc?.join(".") || "?"}: ${e.msg}`);
      showSaveErrors(errors);
      status.textContent = "Validation errors — config was not saved.";
    }
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

function showSaveErrors(errors) {
  let banner = document.getElementById("error-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "error-banner";
    banner.className = "error-banner";
    document.getElementById("content-pane").prepend(banner);
  }
  banner.innerHTML = "<strong>Validation errors:</strong><ul>"
    + errors.map(e => `<li>${e}</li>`).join("")
    + "</ul>";
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

async function init() {
  // Load schema, current mimirheim config, helper configs, and helper schemas
  // in parallel. All four responses are needed before any tab can render.
  const [schemaResp, configResp, helperConfigsResp, helperSchemasResp] = await Promise.all([
    fetch("api/schema"),
    fetch("api/config"),
    fetch("api/helper-configs"),
    fetch("api/helper-schemas"),
  ]);
  gSchema = await schemaResp.json();
  const configData = await configResp.json();
  gConfig = configData.config || {};
  gMqttEnv = configData.mqtt_env || {};
  gReportsAvailable = configData.reports_available || false;
  gHelperConfigs = await helperConfigsResp.json();
  gHelperSchemas = await helperSchemasResp.json();

  // Device tabs — mimirheim.yaml sections.
  registerTab("General",         renderGeneralTab);
  registerTab("Batteries",       (el) => BatteriesCrud.render(el));
  registerTab("PV Arrays",       (el) => PvArraysCrud.render(el));
  registerTab("EV Chargers",     (el) => EvChargersCrud.render(el));
  registerTab("Hybrid Inverters",(el) => HybridInvertersCrud.render(el));
  registerTab("Deferrable Loads",(el) => DeferrableLoadsCrud.render(el));
  registerTab("Static Loads",    (el) => StaticLoadsCrud.render(el));
  registerTab("Heating",         renderHeatingTab);

  // Helper tabs grouped under a single dropdown in the tab bar.
  registerGroup("Helpers", [
    { label: "Nordpool",         renderFn: (el) => renderHelperTab(el, "nordpool.yaml") },
    { label: "PV Forecast",      renderFn: (el) => renderHelperTab(el, "pv-fetcher.yaml") },
    { label: "PV ML",            renderFn: (el) => renderHelperTab(el, "pv-ml-learner.yaml") },
    { label: "Baseload Static",  renderFn: (el) => renderHelperTab(el, "baseload-static.yaml") },
    { label: "Baseload HA REST", renderFn: (el) => renderHelperTab(el, "baseload-ha.yaml") },
    { label: "Baseload HA DB",   renderFn: (el) => renderHelperTab(el, "baseload-ha-db.yaml") },
    { label: "Reporter",         renderFn: (el) => renderHelperTab(el, "reporter.yaml") },
    { label: "Scheduler",        renderFn: (el) => renderHelperTab(el, "scheduler.yaml") },
  ]);

  // Build tab bar and activate the hash tab (or first).
  buildTabBar();
  const activeHash = location.hash.slice(1) || (gReportsAvailable ? 'reports' : gTabs[0]?.label);
  if (activeHash === 'reports') {
    setMode('reports');
  } else {
    setMode('config');
    activateTab(activeHash || gTabs[0]?.label);
  }

  // Wire mode buttons.
  document.getElementById('mode-btn-reports').addEventListener('click', () => {
    location.hash = 'reports';
    setMode('reports');
  });
  document.getElementById('mode-btn-config').addEventListener('click', () => {
    const firstTab = gTabs[0]?.label || '';
    location.hash = firstTab;
    setMode('config');
    activateTab(firstTab);
  });

  // Wire save button.
  document.getElementById("save-btn").addEventListener("click", saveConfig);

  // Remove loading message.
  const loading = document.getElementById("loading-msg");
  if (loading) loading.remove();
}

window.addEventListener("hashchange", () => {
  const hash = location.hash.slice(1);
  if (hash === 'reports') {
    setMode('reports');
  } else {
    if (gMode !== 'config') setMode('config');
    activateTab(hash);
  }
});

init().catch(err => {
  document.getElementById("content-pane").innerHTML =
    `<p style="color:red">Failed to load: ${err.message}</p>`;
});
