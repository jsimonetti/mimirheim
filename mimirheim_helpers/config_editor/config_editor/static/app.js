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

/** @type {Array<{label: string, render: function}>} Registered tabs */
const gTabs = [];

// ---------------------------------------------------------------------------
// Utility: resolve a $ref like "#/$defs/BatteryConfig" to its schema object
// ---------------------------------------------------------------------------

function resolveRef(ref) {
  if (!ref || !ref.startsWith("#/$defs/")) return null;
  const name = ref.slice("#/$defs/".length);
  return (gSchema.$defs || {})[name] || null;
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
    if (fieldSchema.default !== undefined && fieldSchema.default !== null) input.placeholder = String(fieldSchema.default);
  } else if (effectiveSchema.enum) {
    input = document.createElement("select");
    input.name = bindPath;
    input.id = `field-${bindPath}`;
    if (isNullable) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = (fieldSchema.default !== undefined && fieldSchema.default !== null)
        ? `(default: ${fieldSchema.default})`
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
    if (fieldSchema.default !== undefined && fieldSchema.default !== null) input.placeholder = String(fieldSchema.default);
  }

  row.appendChild(input);

  if (fieldSchema.ui_unit) {
    const unit = document.createElement("span");
    unit.className = "field-unit";
    unit.textContent = fieldSchema.ui_unit;
    row.appendChild(unit);
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
  for (const sl of formEl.querySelectorAll(".sublist-container")) {
    const path = sl.dataset.bindPath;
    if (path && sl._liveData !== undefined) {
      setNestedValue(result, path.split("."), sl._liveData);
    }
  }
  return result;
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
  const mqttForm = buildForm("MqttConfig", gConfig.mqtt || {}, "mqtt");
  mqttSection.appendChild(mqttForm);
  // Wire changes.
  mqttForm.addEventListener("change", () => {
    gConfig.mqtt = collectFormData(mqttForm).mqtt;
  });
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
    const defName = schema.title || filename;
    const formEl = buildHelperForm(schema, state.config || {}, filename);
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
      body = { enabled: true, config: cfg };
    }
    try {
      const resp = await fetch(`/api/helper-config/${filename}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (data.ok) {
        statusSpan.textContent = "Saved. Container restart required for s6 service changes.";
        // Refresh state.
        const refreshed = await fetch("/api/helper-configs");
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
      formArea.appendChild(buildHelperForm(schema, existingConfig, selectedFile));
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
        await fetch(`/api/helper-config/${f}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: false }),
        });
      }
      statusSpan.textContent = "Disabled.";
      const refreshed = await fetch("/api/helper-configs");
      gHelperConfigs = await refreshed.json();
      saveBtn.disabled = false;
      return;
    }
    const formEl = formArea.querySelector("div.helper-form");
    const cfg = formEl ? collectHelperFormData(formEl, gHelperSchemas[selectedFile]) : {};
    body = { enabled: true, config: cfg };
    try {
      const resp = await fetch(`/api/helper-config/${selectedFile}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (data.ok) {
        statusSpan.textContent = "Saved. Container restart required.";
        const refreshed = await fetch("/api/helper-configs");
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
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(gConfig),
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
    fetch("/api/schema"),
    fetch("/api/config"),
    fetch("/api/helper-configs"),
    fetch("/api/helper-schemas"),
  ]);
  gSchema = await schemaResp.json();
  const configData = await configResp.json();
  gConfig = configData.config || {};
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
  const activeLabel = location.hash.slice(1) || gTabs[0]?.label;
  activateTab(activeLabel);

  // Wire save button.
  document.getElementById("save-btn").addEventListener("click", saveConfig);

  // Remove loading message.
  const loading = document.getElementById("loading-msg");
  if (loading) loading.remove();
}

window.addEventListener("hashchange", () => {
  activateTab(location.hash.slice(1));
});

init().catch(err => {
  document.getElementById("content-pane").innerHTML =
    `<p style="color:red">Failed to load: ${err.message}</p>`;
});
