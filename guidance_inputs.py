"""Bit-exact CPU preparation for torchvision's fixed RGBA8-derived flow input.

No GPU state or prediction cache. Lookup values come from the original Torch
arithmetic and torchvision transform, not a reimplementation of its rounding.
Models owns fresh resized RGB arrays; identity safely tracks the previous one.
"""


def prepare_flow(self, small):
    np, torch = self.np, self.torch
    lut = getattr(self, '_flow_normalization_lut', None)
    if lut is None:
        ramp = torch.arange(256, dtype=torch.float32).reshape(1, 1, 1, 256).expand(1, 3, 1, 256) / 255.0
        normalized, _ = self.transforms(ramp, ramp)
        lut = normalized[0, :, 0].numpy().copy()
        self._flow_normalization_lut = lut

    def convert(rgb):
        chw = np.empty((1, 3, *rgb.shape[:2]), dtype=np.float32)
        for channel in range(3):
            np.take(lut[channel], rgb[..., channel], out=chw[0, channel])
        return torch.from_numpy(chw)

    previous = getattr(self, '_flow_prepared_tensor', None)
    if getattr(self, '_flow_prepared_source', None) is not self.prev:
        previous = None
    if previous is None:
        previous = convert(self.prev)
    current = convert(small)
    self._flow_prepared_source = small
    self._flow_prepared_tensor = current
    return (previous, current) if self.settings.get('guidance_flow_direction', 'backward') == 'forward_negated' else (current, previous)


def clear_flow_inputs(model):
    model._flow_prepared_source = model._flow_prepared_tensor = None
    model._flow_normalization_lut = None
