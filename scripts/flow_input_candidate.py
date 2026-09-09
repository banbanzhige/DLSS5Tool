"""Experimental CPU input preparation; no model/precision/iteration changes.

The 256-entry lookup is derived using the ORIGINAL torchvision transform and
Torch arithmetic. It therefore preserves float32 bit patterns, including any
division rounding. Only valid for RGBA8-derived RGB uint8 with that transform.
One-frame reuse is input-object scoped, not a persistent inference cache.
"""


def original_flow_input(self, small):
    """Independent pre-optimization reference retained for regression probes."""
    np, torch = self.np, self.torch
    tensors = [torch.from_numpy(np.ascontiguousarray(x)).permute(2, 0, 1).float()[None] / 255.0
               for x in (self.prev, small)]
    previous, current = self.transforms(*tensors)
    return (previous, current) if self.settings.get('guidance_flow_direction', 'backward') == 'forward_negated' else (current, previous)


from dlss5tool.guidance_inputs import prepare_flow
