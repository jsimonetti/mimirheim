/**
 * config-editor-v2 frontend.
 *
 * Deliberately minimal: this proves the registry -> adapter -> Jedison ->
 * save pipeline works end to end in a browser. It is not a polished editor
 * UI. On load it fetches every registered entry's schema (GET
 * /api/entries), builds one Bootstrap nav-tab per entry, and renders a
 * Jedison form for a tab the first time it is selected (GET
 * /api/entries/{name}/data). Saving (POST /api/save) submits data only for
 * entries the user has actually visited (loaded) in this session -- an
 * entry never opened is left out of the request body entirely, rather than
 * being fetched and included just to satisfy the old, since-fixed
 * assumption that every submitted entry's own defaults always validate.
 * See save.validate_all's "untouched entry is excluded, not defaulted"
 * contract in IMPLEMENTATION_DETAILS.md's "Save semantics".
 *
 * What this script does not do:
 * - It does not implement client-side validation. The only validation that
 *   matters is the real Pydantic model's, applied server-side on save; see
 *   IMPLEMENTATION_DETAILS.md, "Why validation always goes through the real
 *   Pydantic model".
 * - It does not persist any state across a page reload.
 */
(function () {
  "use strict";

  var tabsEl = document.getElementById("entry-tabs");
  var panelsEl = document.getElementById("entry-panels");
  var saveButton = document.getElementById("save-button");
  var saveStatus = document.getElementById("save-status");

  // entryName -> { schema, jedison: Jedison.Create instance | null, loaded }
  var entries = new Map();

  function slug(name) {
    return "entry-" + name.replace(/[^a-zA-Z0-9]+/g, "-");
  }

  function fetchJson(url, options) {
    return fetch(url, options).then(function (response) {
      return response
        .json()
        .catch(function () {
          return null;
        })
        .then(function (body) {
          return { status: response.status, body: body };
        });
    });
  }

  function buildTabs(entryList) {
    tabsEl.innerHTML = "";
    panelsEl.innerHTML = "";
    entries.clear();

    entryList.forEach(function (entry, index) {
      var id = slug(entry.name);
      entries.set(entry.name, { schema: entry.schema, jedison: null, loaded: false });

      var tabItem = document.createElement("li");
      tabItem.className = "nav-item";
      tabItem.setAttribute("role", "presentation");

      var tabButton = document.createElement("button");
      tabButton.className = "nav-link" + (index === 0 ? " active" : "");
      tabButton.id = id + "-tab";
      tabButton.type = "button";
      tabButton.textContent = entry.name;
      tabButton.addEventListener("click", function () {
        activateTab(entry.name);
      });
      tabItem.appendChild(tabButton);
      tabsEl.appendChild(tabItem);

      var panel = document.createElement("div");
      panel.className = "tab-pane" + (index === 0 ? " show active" : "");
      panel.id = id + "-panel";

      var errorBox = document.createElement("div");
      errorBox.className = "alert alert-danger d-none";
      errorBox.id = id + "-errors";
      panel.appendChild(errorBox);

      var formContainer = document.createElement("div");
      formContainer.className = "jedison-form-container";
      panel.appendChild(formContainer);

      panelsEl.appendChild(panel);
    });

    if (entryList.length > 0) {
      activateTab(entryList[0].name);
    }
  }

  function setActivePanel(name) {
    entries.forEach(function (_state, entryName) {
      var id = slug(entryName);
      var tabButton = document.getElementById(id + "-tab");
      var panel = document.getElementById(id + "-panel");
      var isActive = entryName === name;
      tabButton.classList.toggle("active", isActive);
      panel.classList.toggle("show", isActive);
      panel.classList.toggle("active", isActive);
    });
  }

  function activateTab(name) {
    setActivePanel(name);
    return ensureLoaded(name);
  }

  function ensureLoaded(name) {
    var state = entries.get(name);
    if (!state || state.loaded) {
      return Promise.resolve();
    }
    return fetchJson("/api/entries/" + encodeURIComponent(name) + "/data").then(function (result) {
      if (result.status !== 200) {
        // A non-200 body (for example, a 422 from an on-disk file that no
        // longer validates against its model) is an error payload, not
        // form data -- rendering it as `data` would silently hand Jedison
        // something like {"errors": {...}} with none of the keys its
        // schema expects. state.loaded is deliberately left false so
        // reactivating this tab (after the on-disk file is fixed) retries
        // the fetch instead of being treated as already loaded.
        showLoadErrorForEntry(name, result);
        return;
      }
      return renderForm(name, result.body || {});
    });
  }

  function showLoadErrorForEntry(name, result) {
    var grouped = result.body && result.body.errors;
    var errors = grouped && grouped[name];
    if (errors) {
      showErrorsForEntry(name, errors);
      return;
    }
    var message =
      (result.body && result.body.error) || "Failed to load (HTTP " + result.status + ").";
    showErrorsForEntry(name, [{ loc: [], message: message }]);
  }

  function renderForm(name, data) {
    var state = entries.get(name);
    var id = slug(name);
    var container = document
      .getElementById(id + "-panel")
      .querySelector(".jedison-form-container");
    // Jedison never dereferences $ref/$defs on its own, so every nested
    // sub-model field needs an explicit RefParser or it renders as a bare
    // type-switcher instead of its real fields. See
    // IMPLEMENTATION_DETAILS.md, "Rendering library".
    var refParser = new window.Jedison.RefParser();
    return refParser.dereference(state.schema).then(function () {
      state.jedison = new window.Jedison.Create({
        container: container,
        refParser: refParser,
        // ThemeBootstrap5 is required for both function and styling: the
        // bare Theme base class throws on first use (this.theme is null
        // internally) and, separately, is not what applies Bootstrap 5's
        // form-control classes. See IMPLEMENTATION_DETAILS.md.
        theme: new window.Jedison.ThemeBootstrap5(),
        schema: state.schema,
        data: data,
        embedSwitcher: true,
        arrayMove: false,
        objectAdd: false,
      });
      state.loaded = true;
    });
  }

  function loadEntries() {
    return fetchJson("/api/entries").then(function (result) {
      buildTabs(result.body || []);
    });
  }

  function clearErrors() {
    entries.forEach(function (_state, entryName) {
      var errorBox = document.getElementById(slug(entryName) + "-errors");
      if (errorBox) {
        errorBox.classList.add("d-none");
        errorBox.innerHTML = "";
      }
    });
  }

  function showErrorsForEntry(entryName, errors) {
    var errorBox = document.getElementById(slug(entryName) + "-errors");
    if (!errorBox) {
      return;
    }
    errorBox.classList.remove("d-none");
    errorBox.innerHTML = "";
    var list = document.createElement("ul");
    list.className = "mb-0";
    errors.forEach(function (error) {
      var item = document.createElement("li");
      item.textContent = error.loc.length ? error.loc.join(".") + ": " + error.message : error.message;
      list.appendChild(item);
    });
    errorBox.appendChild(list);
  }

  function save() {
    clearErrors();
    saveStatus.textContent = "Saving...";

    // Submit data only for entries the user actually visited (loaded) in
    // this session. An entry never opened is left out of the payload
    // entirely -- it is untouched, and save.validate_all treats an entry
    // absent from the payload as excluded from this save rather than as a
    // blocking validation failure. Fetching and including it here, as this
    // used to do, would defeat that: every real registered model requires
    // at least an `mqtt` block with no default, so pre-fetching an
    // untouched entry just to fill in the payload reintroduces the bug
    // this behaviour fixes.
    var payload = {};
    entries.forEach(function (state, name) {
      if (state.loaded) {
        payload[name] = state.jedison ? state.jedison.getValue() : {};
      }
    });

    return fetchJson("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (result) {
      if (result.status === 200) {
        saveStatus.textContent = "Saved.";
        return;
      }
      if (result.status === 422 && result.body && result.body.errors) {
        saveStatus.textContent = "Save failed: see errors below.";
        var entryNames = Object.keys(result.body.errors);
        entryNames.forEach(function (entryName) {
          showErrorsForEntry(entryName, result.body.errors[entryName]);
        });
        if (entryNames.length > 0) {
          return activateTab(entryNames[0]);
        }
        return;
      }
      saveStatus.textContent = "Save failed.";
    });
  }

  saveButton.addEventListener("click", function () {
    save();
  });

  loadEntries();
})();
