# Named Collections replace on write; they are not merged key by key

`overlay_values` merges a dict-valued field key by key today, so an update
to one entry leaves unrelated sibling keys untouched — correct for editing
a single existing entry, but it has no way to express "this key was
removed": a key simply absent from Candidate Values is left in place
forever. When full editing (add, remove, rename) of Named Collection
fields ships, we decided such a field is replaced wholesale on write,
exactly like Ordered Collection (list) fields already are — the Config
Editor submits the complete surviving set of entries, and the Config Owner
replaces the whole field rather than merging into it.

We rejected a tombstone or delete-marker convention that would let a
partial submission delete a key without resubmitting its untouched
siblings, since that requires a new sentinel-value convention in both
Candidate Values and `overlay_values` for this one Field Shape, where
matching the whole-field-replace behavior Ordered Collections already have
needs no new convention at all.
