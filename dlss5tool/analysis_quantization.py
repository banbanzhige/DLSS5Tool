"""Exact monotone code boundaries for the existing NumPy analysis tone curve."""
import numpy as np


def cpu_fingerprint():
    """Pin the CPU dispatch used to build the exact NumPy power codebook."""
    from numpy._core import _multiarray_umath
    import cv2
    import hashlib
    features = getattr(_multiarray_umath, '__cpu_features__', None)
    if not isinstance(features, dict) or not features:
        raise RuntimeError('NumPy CPU dispatch fingerprint unavailable')
    return dict(numpy=np.__version__, opencv=cv2.__version__,
        numpy_cpu_features={name: bool(value) for name, value in sorted(features.items())},
        opencv_build=hashlib.sha256(cv2.getBuildInformation().encode('utf-8')).hexdigest(),
        opencv_optimized=bool(cv2.useOptimized()),
        opencv_ipp=bool(cv2.ipp.useIPP()), opencv_ipp_not_exact=bool(cv2.ipp.useIPP_NotExact()))


def quantize_linear(values):
    values = np.maximum(values, 0)
    mapped = values/(1.0+values)
    srgb = np.where(mapped <= 0.0031308, mapped*12.92,
                    1.055*np.power(mapped, 1.0/2.4)-0.055)
    return (np.clip(srgb, 0, 1)*255+0.5).astype(np.uint8)


def thresholds(dtype):
    dtype = np.dtype(dtype)
    bits_dtype = np.dtype(np.uint32 if dtype == np.float32 else np.uint64)
    if dtype not in (np.dtype(np.float32), np.dtype(np.float64)):
        raise ValueError('Float32/64 analysis curve required')
    lo = np.zeros(256, bits_dtype)
    hi = np.full(256, np.array([1024], dtype).view(bits_dtype)[0], bits_dtype)
    targets = np.arange(256, dtype=np.uint16)
    while np.any(lo < hi):
        mid = lo+(hi-lo)//2
        # Repeat complete vector blocks so the CPU reference uses its ordinary
        # contiguous-array power kernel, including all output code boundaries.
        codes = quantize_linear(np.repeat(mid.view(dtype), 16)).reshape(256, 16)[:, 0]
        lower = codes < targets
        lo = np.where(lower, mid+1, lo)
        hi = np.where(lower, hi, mid)
    lo[0] = 0
    # NumPy's vector power can cross back over an 8-bit boundary by one ULP.
    # Preserve those finite bit patterns explicitly instead of assuming strict
    # monotonicity or accepting a one-code tolerance. Enumerate a wide window
    # around every boundary using the same contiguous vector reference.
    corrections = {}
    offsets = np.arange(-8192, 8193, dtype=np.int64)
    for start in range(1, 256, 8):
        nearby = (lo[start:start+8].astype(np.int64)[:, None]+offsets).clip(0).astype(bits_dtype).ravel()
        expected = quantize_linear(np.repeat(nearby.view(dtype), 16)).reshape(-1, 16)[:, 0]
        actual = np.searchsorted(lo, nearby, side='right')-1
        for key, value in zip(nearby[expected != actual], expected[expected != actual]):
            corrections[int(key)] = int(value)
    keys = np.array(sorted(corrections), bits_dtype)
    values = np.array([corrections[int(key)] for key in keys], np.uint8)
    for value in (lo, keys, values): value.flags.writeable = False
    return lo, keys, values
