# A nested model's FormSpec is owned by the package that defines the model

Some nested validation models are shared across Config Owners: every
helper's own config nests `MqttConfig` and `HomeAssistantConfig` from
`helper_common`, for instance. We decided that a nested model's FormSpec is
authored once, in the same package that defines the model, and referenced
by every FormSpec that nests it — `helper_common` owns the FormSpecs for
its own shared nested models, mimirheim core owns the FormSpecs for its
own. We rejected letting each consuming FormSpec author its own copy of a
shared nested model's presentation, since that duplicates the same labels
and help text across every helper and lets them drift out of sync with
each other and with the model they describe.

Core and helpers do not currently share any validation model classes with
each other (each defines its own `MqttConfig`, for example), so this
convention does not yet require a FormSpec to be authored outside the
Config Owner's own package tree — it only applies within the helper
ecosystem today.
