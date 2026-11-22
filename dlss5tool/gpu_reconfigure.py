"""Local ownership registry for remapping acknowledged NR shared buffers.

Only mappings created in this process participate. Foreign/encoder importers
must explicitly retire their resources; a raw handle is never proof of idleness.
"""
import weakref


_mappings = {}


def register(mapping):
    key = mapping.producer_pid, mapping.handle
    _mappings.setdefault(key, weakref.WeakSet()).add(mapping)


def unregister(mapping):
    key = getattr(mapping, 'producer_pid', None), getattr(mapping, 'handle', None)
    entries = _mappings.get(key)
    if entries is not None:
        entries.discard(mapping)
        if not entries:
            _mappings.pop(key, None)


def require_retired(owner):
    for kind in ('input', 'output'):
        descriptor = getattr(owner, '_gpu_'+kind+'_descriptor', None)
        if descriptor:
            for mapping in list(_mappings.get((descriptor.get('producer_pid'), descriptor.get('handle')), ())):
                if mapping.pointer.value or mapping.external.value or mapping._image_owners:
                    raise RuntimeError('Retire shared GPU images and mappings before releasing exports')


def prepare(owner):
    """Check every consumer first, then suspend complete mapping generations."""
    from dlss5tool.dlss_host_process import HostProcessError
    groups = []
    for kind in ('input', 'output'):
        descriptor = getattr(owner, '_gpu_'+kind+'_descriptor', None)
        if not descriptor:
            continue
        if 'producer_pid' not in descriptor:
            raise HostProcessError('Retire unmanaged GPU consumers before reconfiguration')
        entries = list(_mappings.get((descriptor['producer_pid'], descriptor['handle']), ()))
        if not entries:
            raise HostProcessError('Retire unmanaged GPU '+kind+' consumers before reconfiguration')
        for mapping in entries:
            mapping.check_reconfigure()
        groups.append((kind, entries))
    try:
        for _, entries in groups:
            for mapping in entries:
                mapping.suspend_reconfigure()
    except BaseException as error:
        retire_failed(owner, groups, error)
        raise
    return groups


def resume(owner, groups):
    """Publish new handle/pitch and image geometry to the same local owners."""
    for kind, entries in groups:
        descriptor = getattr(owner, 'gpu_'+kind+'_create')()
        setattr(owner, '_gpu_'+kind+'_descriptor', dict(descriptor))
        for mapping in entries:
            mapping.resume_reconfigure(descriptor, owner._w, owner._h,
                owner.settings.get('frame_format', 'rgba8'), owner.settings.get('color_profile', 'srgb'))


def retire_failed(owner, groups, error):
    for _, entries in groups:
        for mapping in entries:
            mapping._failed = True
            for image in mapping._images:
                image._suspended = True
                image._ready = False
    if getattr(owner, '_session', None) is not None:
        owner._session._reconfigure_failed = True
        try:
            owner._session.close()
        except BaseException as cleanup:
            if hasattr(error, 'add_note'):
                error.add_note('GPU reconfiguration retirement: '+str(cleanup))


def reconfigure(owner, width, height, updated, callback, *, force=False):
    """Cross the local CUDA-consumer barrier before changing native exports."""
    groups = prepare(owner)
    try:
        session = owner._session
        if getattr(session, 'supports_gpu_reconfigure', False):
            session.request('release_gpu_resources')
        else:
            # Older native DLLs have no explicit export retirement API. After
            # local imports retire, their normal process replacement is safe.
            session.supports_reconfigure = False
        owner._gpu_input_descriptor = owner._gpu_output_descriptor = None
        callback(owner, width, height, updated, force=force)
        resume(owner, groups)
    except BaseException as error:
        # Import/format failures cannot leave an apparently usable half-remap.
        # Local images remain suspended until their owning pipeline retires.
        retire_failed(owner, groups, error)
        raise
