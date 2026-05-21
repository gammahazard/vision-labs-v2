"""Re-export shim — actual source lives in ../vehicle-attributes/service.py."""
from service import (  # noqa: F401
    handle_event,
    _scale_bbox_sub_to_hd,
    _pad_bbox,
    _crop_hd_frame,
    _open_buffer,
    _accumulate_crop,
    _flush,
    _check_registry_wants_attributes,
    main,
)
