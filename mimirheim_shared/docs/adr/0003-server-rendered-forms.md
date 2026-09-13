# Forms render server-side from FormSpec; no generic client-side schema renderer

config_editor_v2 rendered forms client-side using a generic JSON-Schema-to-
form JS library (Jedison), which required a permanent mapping layer between
the pydantic JSON Schema and the library's expected shape, and carried
unresolved rendering edge cases. Since FormSpec is now explicit about every
presentation concern, we render forms server-side directly from FormSpec
(templates plus minimal JS for interactivity such as conditional visibility
and add/remove rows), removing the need for a generic client-side
form-rendering library and its translation layer entirely.
