# Stub class JSONs

These files are placeholders shipped with the initial Phase 3 commit so
the classifier module has SOMETHING to load against during unit tests
and the disabled-by-default path. They are REPLACED with the real label
lists by `scripts/vehicle_attributes/train_multihead.py` before weights
are uploaded to HF Hub.

The classifier's `_load_classes()` reads whichever version exists in
`/app/classes/` at container start. After a weights download from HF
Hub, the classes/ files in the image are overwritten by whatever the
HF Hub repo ships alongside the safetensors checkpoint.

Order in each list MATCHES the model head's argmax indices.
