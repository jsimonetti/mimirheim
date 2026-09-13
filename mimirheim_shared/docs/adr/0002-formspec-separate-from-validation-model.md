# FormSpec is a separate artifact from the pydantic validation model

The prior config editors (v1 and v2) attached every presentation concern —
labels, help text, tiering — onto the pydantic model itself via
`json_schema_extra`. We decided to split these: the validation model stays
pure, and a separate FormSpec object, colocated with the model, carries all
presentation concerns instead. We rejected keeping them combined because it
couples validation and presentation in one artifact, forcing the schema
module to keep growing a presentation-specific `x-` vocabulary and a
JSON-Schema-to-form mapping layer for every new UI concept.
