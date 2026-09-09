"""Bounded BGR visualizations of the actual normalized guidance buffers."""
import cv2
import numpy as np
from dlss5tool.guidance_parameters import parameters


FLOW_RANGE = 32.0  # pixels per frame at the displayed inference resolution


def guidance_images(motion, depth, mode, settings=None):
    display = parameters(settings or {}, strict=True)
    images = {}
    if mode in (2, 3):
        if not np.isfinite(depth).all():
            raise ValueError('Non-finite depth preview')
        gray = np.rint(np.clip(depth, 0, 1) * 255).astype(np.uint8)
        if display['guidance_depth_invert']:
            gray = 255 - gray
        images['depth'] = (cv2.applyColorMap(gray, cv2.COLORMAP_TURBO)
                           if display['guidance_depth_palette'] == 'turbo'
                           else cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
    if mode in (1, 3):
        if not np.isfinite(motion).all():
            raise ValueError('Non-finite flow preview')
        x, y = motion[..., 0], motion[..., 1]
        angle = np.mod(np.arctan2(y, x), 2 * np.pi)
        hsv = np.empty((*motion.shape[:2], 3), np.uint8)
        hsv[..., 0] = np.rint(angle * (179 / (2 * np.pi))).astype(np.uint8)
        hsv[..., 1] = 255
        hsv[..., 2] = np.rint(np.clip(np.hypot(x, y) / display['guidance_flow_range'], 0, 1) * 255).astype(np.uint8)
        images['flow'] = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    return images
