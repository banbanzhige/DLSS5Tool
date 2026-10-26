"""Conservative admission for bounded CUDA/D3D consumers on their selected GPU."""
import ctypes as C

from dlss5tool.cuda_session import bind, check

MIB = 1024**2


def estimate_vram(width, height, *, scale=1, hdr=False, nr=False, multiplier=1,
                  super_resolution=True, encoder=False):
    if (type(width) is not int or type(height) is not int or min(width, height) <= 0
            or scale not in (1, 2, 4) or multiplier not in (1, 2, 3, 4)):
        raise ValueError('Bounded device geometry and multiplier required')
    source, output = width*height, width*height*scale*scale
    # Include model/driver reserves as well as pitched images and shared D3D
    # resources. These are admission estimates, not measured allocation totals.
    amount = 256*MIB + output*(16 if hdr else 8)
    if super_resolution:
        amount += 512*MIB + source*96 + output*(48 if hdr else 24)
    if encoder:
        amount += 256*MIB + output*24
    if nr or multiplier > 1:
        amount += 64*MIB + output*16  # exact scene proxy and flow preparation
    if nr:
        amount += 768*MIB + output*128
    if multiplier > 1:
        amount += 1024*MIB + output*160  # NVOFA, history and FG native resources
    return amount


def require_vram(session, required):
    """Query the current owner context, never an unrelated default GPU."""
    available, capacity = C.c_size_t(), C.c_size_t()
    with session.current():
        check(bind(session.cuda, 'cuMemGetInfo_v2',
                   [C.POINTER(C.c_size_t), C.POINTER(C.c_size_t)])(
                       C.byref(available), C.byref(capacity)), 'Device memory preflight')
    if available.value < required:
        raise MemoryError('Insufficient VRAM on the selected GPU for the bounded device pipeline '
                          f'(required {required}, available {available.value} bytes)')
    return dict(required_bytes=required, free_bytes=available.value, total_bytes=capacity.value)


def admit_plan(session, plan, *, encoder=False):
    return require_vram(session, estimate_vram(plan['width'], plan['height'], scale=plan['scale'],
        hdr=plan.get('color_profile', 'srgb') != 'srgb', nr=plan.get('nr', False),
        multiplier=plan.get('multiplier', 1), encoder=encoder))
