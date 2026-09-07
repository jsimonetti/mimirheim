/**
 * mimirheim config editor -- the "mimir-topic-placeholder" custom Jedison editor.
 *
 * SPEC.md §4/§6: a field whose default MQTT topic is auto-derived at runtime
 * from a per-entry map key (e.g. "{mimir_topic_prefix}/input/pv/{array_key}/forecast")
 * cannot use x-watch/x-template (IMPLEMENTATION_DETAILS.md: x-template cannot
 * reach a map entry's own key). Instead the field's value stays null
 * (meaning "auto-derive at runtime", already understood by the Pydantic/
 * runtime side) and this editor shows the live-computed topic as the
 * underlying <input>'s placeholder text only -- it never calls setValue()
 * on the user's behalf.
 *
 * The concrete topic template for a given field is not carried in a
 * dedicated schema key (SPEC.md §4 deliberately allows no custom
 * json_schema_extra key beyond x-mimirheim). Every bundled field using this
 * editor instead documents its own template inside its ordinary "description"
 * string, in the fixed, machine-parseable form
 * "Defaults to '<template>' when not set." -- verified against every current
 * bundled schema before relying on it here. A description that does not
 * match this form degrades to "no placeholder" rather than throwing.
 */
"use strict";

/**
 * Build the "mimir-topic-placeholder" custom editor class for a given
 * Jedison library instance.
 *
 * A factory, not a class definition at module scope, so this file has no
 * load-order dependency on when the Jedison UMD script tag runs -- app.js
 * calls this once, after both scripts are loaded, and passes the result
 * straight into Jedison.Create's customEditors option.
 *
 * @param {object} Jedison  The global Jedison object (window.Jedison).
 * @returns {Function} A class extending Jedison.EditorString.
 */
function createMimirTopicPlaceholderEditor(Jedison) {
  const TEMPLATE_PATTERN = /Defaults to '([^']+)' when not set\./;

  // Tokens a bundled schema's description may use, and how to resolve each to
  // a live value from the currently-open entry's own Jedison document.
  //   - {mimir_topic_prefix}: the injected, read-only context snapshot
  //     (SPEC.md §5) -- only present for entries other than mimirheim.yaml.
  //   - {mqtt.topic_prefix}: the entry's own "mqtt" section (mimirheim.yaml's
  //     own topic-placeholder fields have no injected context and read their
  //     own document's mqtt.topic_prefix instead).
  const PREFIX_TOKEN_PATHS = {
    "{mimir_topic_prefix}": "#/context/mqtt_topic_prefix",
    "{mqtt.topic_prefix}": "#/mqtt/topic_prefix",
  };
  const KEY_TOKENS = ["{array_key}", "{name}"];

  /**
   * Read a document-absolute path's current value, or undefined if that
   * instance does not exist in this document (e.g. no injected context on
   * mimirheim.yaml's own entry).
   *
   * @param {object} jedison  The root Jedison.Create instance.
   * @param {string} path     A document-absolute path, e.g. "#/mqtt/topic_prefix".
   * @returns {*}
   */
  function readInstanceValue(jedison, path) {
    const target = jedison.getInstance(path);
    return target ? target.getValue() : undefined;
  }

  /**
   * True when `schema` describes a dynamic, named-map object (e.g.
   * "batteries": {additionalProperties: {$ref: BatteryConfig}}), whose
   * children's own instance keys are user-chosen entry names -- as opposed
   * to a fixed-shape object (e.g. "outputs": {properties: {...}}), whose
   * child keys are just that schema's literal property names.
   *
   * @param {object} schema
   * @returns {boolean}
   */
  function isDynamicMapSchema(schema) {
    return !!schema && typeof schema.additionalProperties === "object" && schema.additionalProperties !== null;
  }

  /**
   * Find this field's own per-entry map key -- SPEC.md/IMPLEMENTATION_DETAILS.md
   * describe this as simply `instance.parent.getKey()`, which is correct when
   * the field sits directly on the map entry's own config object (e.g.
   * pv_arrays.<key>.topic_forecast). Several bundled fields instead sit one
   * level deeper, inside a fixed-shape "outputs"/"inputs" sub-object (e.g.
   * batteries.<key>.outputs.exchange_mode) -- for those, `instance.parent`
   * is the "outputs" instance (whose own key is literally "outputs", not the
   * battery's name), and the real map key is one level further up. Walking
   * up until an ancestor's *parent* turns out to be a dynamic map handles
   * both shapes with no per-field/per-helper special-casing.
   *
   * @param {object} instance  The topic field's own Jedison instance.
   * @returns {string|undefined}
   */
  function findOwnMapKey(instance) {
    let node = instance.parent;
    while (node) {
      const grandparent = node.parent;
      if (grandparent && isDynamicMapSchema(grandparent.schema)) return node.getKey();
      node = grandparent;
    }
    return undefined;
  }

  class MimirTopicPlaceholderEditor extends Jedison.EditorString {
    static resolves(schema) {
      return !!schema && schema["x-format"] === "mimir-topic-placeholder";
    }

    static priority() {
      return 0;
    }

    sanitize(value) {
      return value === "" ? null : String(value);
    }

    build() {
      this.control = this.theme.getInputControl({
        title: this.getTitle(),
        description: this.getDescription(),
        type: "text",
        id: this.getIdFromPath(this.instance.path),
        info: this.getInfo(),
      });
    }

    addEventListeners() {
      const eventType = this.getValidationEventType();
      this.control.input.addEventListener(eventType, () => {
        this.instance.setValue(this.sanitize(this.control.input.value), true, "user");
      });
      // The prefix token can live in this same document (mqtt.topic_prefix);
      // re-render the placeholder as the user edits it live, matching the
      // "live-computed" behaviour SPEC.md §6 describes. The context
      // snapshot's own prefix (the other token source) is fixed for the
      // life of this instance (SPEC.md §5), so no watch is needed for it.
      this.instance.jedison.watch(PREFIX_TOKEN_PATHS["{mqtt.topic_prefix}"], () => {
        if (this.control) this.control.input.placeholder = this._computePlaceholder();
      });
    }

    refreshUI() {
      const value = this.instance.getValue();
      this.control.input.value = value === null || value === undefined ? "" : String(value);
      this.control.input.placeholder = this._computePlaceholder();
    }

    /**
     * Compute the live topic placeholder from this field's own description
     * text, or return "" if the description does not carry a recognisable
     * template (degrade silently, never throw -- SPEC.md §11's "no
     * schema-derived string is unsafely handled" spirit extends to "a
     * malformed description must not break the form").
     *
     * @returns {string}
     */
    _computePlaceholder() {
      const description = this.instance.schema.description || "";
      const match = TEMPLATE_PATTERN.exec(description);
      if (!match) return "";
      let template = match[1];

      const ownKey = findOwnMapKey(this.instance);
      if (ownKey !== undefined) {
        for (const token of KEY_TOKENS) {
          template = template.split(token).join(ownKey);
        }
      }

      for (const [token, path] of Object.entries(PREFIX_TOKEN_PATHS)) {
        if (!template.includes(token)) continue;
        const value = readInstanceValue(this.instance.jedison, path);
        if (value !== undefined && value !== null && value !== "") {
          template = template.split(token).join(String(value));
        }
      }

      // Any token left unresolved (e.g. context absent, or own key
      // unavailable) means the placeholder would be misleading; show
      // nothing rather than a string with literal "{...}" braces in it.
      return /\{[^}]+\}/.test(template) ? "" : template;
    }
  }

  return MimirTopicPlaceholderEditor;
}
