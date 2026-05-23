"""Tests for scripts/vehicle_attributes/finetune_heads.py:_save_merged_multihead.

The actual training loop needs GPU + real crops, so we don't unit-test it.
What we DO test is the multihead-merge save path — body retrain's
deploy-time hazard is that we corrupt or drop make_head/model_head/backbone
tensors when rebuilding the multihead .safetensors. A test that exercises
the merge with synthetic tensors verifies the load→replace→save round-trip
without any GPU or model dependency.
"""

import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts" / "vehicle_attributes"
sys.path.insert(0, str(SCRIPTS_DIR))

# safetensors is a transitive dep of torch; if it's not installed in the
# test env, skip the whole module rather than fail at import.
torch = pytest.importorskip("torch")
safetensors_torch = pytest.importorskip("safetensors.torch")

from finetune_heads import _save_merged_multihead  # noqa: E402


def _fake_multihead(path: Path) -> dict:
    """Build a fake multihead.safetensors with the same key shape as the
    real one — backbone.* + body_head.* + make_head.* + model_head.*.
    Returns the dict it wrote so tests can compare values."""
    state = {
        # A handful of backbone tensors with the convnext_tiny-ish naming
        "backbone.stem.0.weight":     torch.randn(96, 3, 4, 4),
        "backbone.stem.0.bias":       torch.randn(96),
        "backbone.head.norm.weight":  torch.randn(768),
        "backbone.head.norm.bias":    torch.randn(768),
        # Each head: feat_dim=768 → num_classes
        "body_head.weight":   torch.randn(9, 768),  # 9 body classes
        "body_head.bias":     torch.randn(9),
        "make_head.weight":   torch.randn(49, 768),
        "make_head.bias":     torch.randn(49),
        "model_head.weight":  torch.randn(196, 768),
        "model_head.bias":    torch.randn(196),
    }
    safetensors_torch.save_file(state, str(path))
    return state


def _new_body_head_state(num_classes: int = 9) -> dict:
    """Bare-Linear state_dict — what _build_body_model.head.state_dict()
    returns. Keys are 'weight' and 'bias' (no head prefix)."""
    return {
        "weight": torch.randn(num_classes, 768),
        "bias":   torch.randn(num_classes),
    }


def test_merge_replaces_body_head_only(tmp_path):
    """The merged file must have:
      - new body_head.weight + body_head.bias (exact match of the input state)
      - unchanged backbone.* (every tensor bit-identical to source)
      - unchanged make_head.* and model_head.*
    """
    src_path = tmp_path / "multihead_v0.safetensors"
    src_state = _fake_multihead(src_path)

    new_body = _new_body_head_state()
    out_path = tmp_path / "multihead_v1.safetensors"
    _save_merged_multihead(src_path, new_body, out_path)

    out_state = safetensors_torch.load_file(str(out_path))

    # body_head.* should equal the new body state
    assert torch.equal(out_state["body_head.weight"], new_body["weight"])
    assert torch.equal(out_state["body_head.bias"], new_body["bias"])
    # Every other key should be bit-identical to the source
    for k, v in src_state.items():
        if k.startswith("body_head."):
            continue
        assert k in out_state, f"missing key {k} in merged output"
        assert torch.equal(out_state[k], v), f"tensor {k} mutated during merge"
    # No EXTRA keys were introduced
    assert set(out_state.keys()) == set(src_state.keys())


def test_merge_refuses_when_source_has_no_backbone(tmp_path):
    """If the input file has no backbone.* tensors it's not a real multihead
    and merging would silently emit a broken file. _save_merged_multihead
    should refuse."""
    src_path = tmp_path / "broken.safetensors"
    safetensors_torch.save_file({
        "body_head.weight": torch.randn(9, 768),
        "body_head.bias":   torch.randn(9),
        # No backbone, no make_head, no model_head
    }, str(src_path))

    out_path = tmp_path / "merged.safetensors"
    with pytest.raises(ValueError, match="backbone"):
        _save_merged_multihead(src_path, _new_body_head_state(), out_path)


def test_merge_overwrites_pre_existing_body_head_cleanly(tmp_path):
    """Re-running the merge against an already-merged file must not produce
    duplicate body_head.* keys (safetensors disallows that anyway, but the
    code must avoid relying on save_file's error to catch this)."""
    src_path = tmp_path / "multihead_v0.safetensors"
    _fake_multihead(src_path)
    first_body = _new_body_head_state()
    second_body = _new_body_head_state()

    intermediate = tmp_path / "intermediate.safetensors"
    _save_merged_multihead(src_path, first_body, intermediate)

    # Now merge again with a DIFFERENT body head against the intermediate
    out_path = tmp_path / "final.safetensors"
    _save_merged_multihead(intermediate, second_body, out_path)

    out_state = safetensors_torch.load_file(str(out_path))
    # The second merge's body wins
    assert torch.equal(out_state["body_head.weight"], second_body["weight"])
    assert torch.equal(out_state["body_head.bias"], second_body["bias"])
    # The first merge's body is NOT in the file
    assert not torch.equal(out_state["body_head.weight"], first_body["weight"])
