"""Owned shared-frame handoff primitives for an isolated NR worker.

The parent owns named buffers. Peers own only their mapped handles. A candidate
must stay alive after dispatch until an ACK or confirmed worker retirement.
"""
from multiprocessing import shared_memory
from collections import deque
import os

import numpy as np


class FramePair:
    def __init__(self, width, height, dtype, *, names=None, borrow=None):
        from dlss5tool.super_resolution import validate_dimensions
        validate_dimensions(width, height, 1)
        self.input_memory = self.output_memory = None
        self.input_frame = self.output_frame = None
        self.owns_handles = borrow is None
        self.owns_names = names is None and borrow is None
        try:
            size = width*height*4*np.dtype(dtype).itemsize
            if borrow is not None:
                self.input_memory, self.output_memory = borrow
            else:
                for index, field in enumerate(('input_memory', 'output_memory')):
                    options = dict(create=True, size=size) if names is None else dict(name=names[index])
                    try:
                        memory = shared_memory.SharedMemory(**options, track=self.owns_names)
                    except TypeError:
                        memory = shared_memory.SharedMemory(**options)
                    setattr(self, field, memory)
            if min(self.input_memory.size, self.output_memory.size) < size:
                raise ValueError('Reconfiguration frame buffer is too small')
            self.input_frame = np.ndarray((height, width, 4), dtype, buffer=self.input_memory.buf)
            self.output_frame = np.ndarray((height, width, 4), dtype, buffer=self.output_memory.buf)
        except BaseException:
            self.close()
            raise

    def detach(self):
        """Transfer both mapped handles and views to an explicit new owner."""
        result = self.input_memory, self.output_memory, self.input_frame, self.output_frame
        self.input_memory = self.output_memory = None
        self.input_frame = self.output_frame = None
        return result

    def close(self):
        self.input_frame = self.output_frame = None
        for field in ('input_memory', 'output_memory'):
            memory = getattr(self, field)
            if memory is not None:
                if self.owns_handles:
                    memory.close()
                    if self.owns_names:
                        try:
                            memory.unlink()
                        except FileNotFoundError:
                            pass
                setattr(self, field, None)


def worker_candidate(live, message, generation):
    """Validate/map first; a caller must retire the worker on native failure."""
    from dlss5tool import dlss_engine, guidance_client
    from dlss5tool.super_resolution import validate_dimensions
    width, height = message['width'], message['height']
    validate_dimensions(width, height, 1)
    if message.get('generation') != generation+1:
        raise ValueError('Reconfiguration generation mismatch')
    if live.pending:
        raise ValueError('Drain DLSS frames before reconfiguration')
    settings = guidance_client.normalize_public_settings(message['settings'])
    candidate = FramePair(width, height, dlss_engine.frame_dtype(settings),
                          names=(message['input_name'], message['output_name']))
    try:
        live.reconfigure(width, height, settings, force=bool(message.get('force')))
    except BaseException:
        candidate.close()
        raise
    return candidate


def reconfigure_session(session, width, height, settings, *, force=False):
    """Prepare/adopt/commit a parent frame pair using the worker's generation ACK.

    A dispatched failure retires the existing owner before releasing either
    generation. Failed retirement retains allocation credit and all mappings.
    """
    from dlss5tool import dlss_engine, guidance_client
    from dlss5tool.host_queue import configured_in_flight
    from dlss5tool.memory_budget import memory_allocation
    from dlss5tool.super_resolution import validate_dimensions
    validate_dimensions(width, height, 1)
    settings = guidance_client.normalize_public_settings(settings)
    for retired in list(getattr(session, '_reconfigure_frames', ())):
        retired.close()
        session._reconfigure_frames.remove(retired)
    dtype = dlss_engine.frame_dtype(settings)
    size = width*height*4*np.dtype(dtype).itemsize
    reuse = min(session._input_memory.size, session._output_memory.size) >= size
    required = size*(2*configured_in_flight(settings)+(0 if reuse else 2))
    with memory_allocation(required, stage='nr_reconfigure') as ticket:
        candidate = FramePair(width, height, dtype, borrow=(session._input_memory, session._output_memory) if reuse else None)
        pending = session.__dict__.setdefault('_reconfigure_frames', [])
        try:
            if ticket and not reuse:
                ticket.consume(size*2)
            pending.append(candidate)
        except BaseException:
            candidate.close()
            raise
        generation = getattr(session, 'configuration_generation', 0)+1
        try:
            response = session.request('reconfigure', width=width, height=height,
                settings=settings, input_name=candidate.input_memory.name,
                output_name=candidate.output_memory.name, generation=generation, force=force)
            if response.get('generation') != generation:
                raise RuntimeError('DLSS reconfiguration acknowledgement mismatch')
        except BaseException as error:
            session._reconfigure_failed = True
            try:
                session.close()
            except BaseException as cleanup:
                if ticket:
                    ticket.defer_until_retired(session)
                if hasattr(error, 'add_note'):
                    error.add_note('Reconfiguration retirement: '+str(cleanup))
            raise
        old_input, old_output = session._input_memory, session._output_memory
        session._input_memory, session._output_memory, session._input_frame, session._output_frame = candidate.detach()
        session.width, session.height, session.dtype = width, height, dtype
        session.configuration_generation = generation
        pending.remove(candidate)
        if not reuse:
            previous = FramePair.__new__(FramePair)
            previous.input_memory, previous.output_memory = old_input, old_output
            previous.input_frame = previous.output_frame = None
            previous.owns_handles = previous.owns_names = True
            try:
                previous.close()
            except Exception as error:
                pending.append(previous)
                from dlss5tool.memory_budget import record_memory_event
                record_memory_event('nr_old_map_cleanup_failed', error=str(error))


def retained_outputs(owner):
    return owner.__dict__.setdefault('_drained_outputs', deque())


def initialization_changed(owner, updated):
    """Only incompatible initialized identities require a new process."""
    from dlss5tool import dlss_engine, mod_paths
    runtime_changed = any(updated.get(key, '') != owner.settings.get(key, '')
                          for key in ('dlss_runtime', 'mods_directory'))
    if runtime_changed and owner._live_factory is None:
        desired = mod_paths.runtime_info(updated, adapter=owner.adapter_info)['path']
        runtime_changed = os.path.normcase(os.path.realpath(desired)) != os.path.normcase(os.path.realpath(owner.runtime_path))
    gpu = updated.get('render_gpu', dlss_engine.RENDER_GPU_AUTO)
    gpu_changed = gpu != owner.settings.get('render_gpu', dlss_engine.RENDER_GPU_AUTO)
    if gpu_changed and gpu == owner.adapter_info.get('id'):
        gpu_changed = False
    if gpu_changed and gpu == dlss_engine.RENDER_GPU_AUTO and owner.adapter_info.get('preference_index') == 0:
        gpu_changed = False
    return (runtime_changed or gpu_changed
            or updated.get('_gpu_export_host') != owner.settings.get('_gpu_export_host')
            or owner._requires_replacement(str(updated.get('host_backend', owner.preference)))
            or (dlss_engine.frame_format_id(updated) == 1 and owner.backend not in ('v2', 'bypass')))


def reconfigure_proxy(owner, width, height, updated, *, force=False):
    """Orchestrate one configuration using already-tested session handoff.

    Accepted asynchronous frames are copied out in order before geometry
    changes. Queued copies remain available even if the new mode is synchronous.
    """
    from dlss5tool import appearance, dlss_engine, guidance_client
    from dlss5tool.dlss_host_process import HostProcessError
    from dlss5tool.memory_budget import pressure_details
    replace = (owner.bypass_reason != appearance.enhancement_bypass(updated)
               or initialization_changed(owner, updated))
    resource_change = (force or (width, height) != (owner._w, owner._h)
        or dlss_engine.frame_contract(updated) != dlss_engine.frame_contract(owner.settings)
        or guidance_client.contract(updated) != guidance_client.contract(owner.settings)
        or dlss_engine._host_config(updated) != dlss_engine._host_config(owner.settings)
        or updated.get('preset') != owner.settings.get('preset'))
    if (replace or resource_change) and getattr(owner._session, '_validate_configuration', False):
        guidance_client.validate(updated)
    if (replace or resource_change) and (getattr(owner, '_gpu_input_descriptor', None) or getattr(owner, '_gpu_output_descriptor', None)):
        from dlss5tool.gpu_reconfigure import reconfigure
        return reconfigure(owner, width, height, updated, reconfigure_proxy, force=force)
    if replace or resource_change:
        if owner._session is None:
            raise HostProcessError('DLSS 会话正在关闭，请先重试关闭')
        owner._retry_retiring()
        count = int(owner._session.request('pending')['pending'])
        if not 0 <= count <= owner.max_in_flight:
            raise HostProcessError('Invalid pending DLSS frame count')
        if count and owner.bypass_reason != appearance.enhancement_bypass(updated) and not getattr(owner._session, 'supports_reconfigure', False):
            raise HostProcessError('Drain pending frames before changing enhancement bypass')
        for _ in range(count):
            from dlss5tool.memory_budget import memory_allocation
            # Reserve the destination before removing an accepted native frame.
            # Admission/allocation failure must leave it available to dequeue.
            with memory_allocation(owner._session._output_frame.nbytes, stage='nr_drain') as ticket:
                output = np.empty_like(owner._session._output_frame)
                if ticket:
                    ticket.consume(output.nbytes)
                retained_outputs(owner).append(output)
                try:
                    response = owner._session.request('dequeue')
                    if not response['has_output']:
                        raise HostProcessError('DLSS frame drain failed')
                    np.copyto(output, owner._session._output_frame)
                except BaseException:
                    retained_outputs(owner).pop()
                    raise
        if replace or not getattr(owner._session, 'supports_reconfigure', False):
            owner._replace(width, height, updated)
            return
        worker_settings = dict(updated, host_backend=owner.backend)
        try:
            owner._session.reconfigure(width, height, worker_settings, force=force)
        except Exception as error:
            if not getattr(owner._session, '_reconfigure_failed', False):
                raise
            if pressure_details(error) is not None or isinstance(error, MemoryError):
                raise
            # A retirement failure prevents any additional worker allocation.
            owner._session.close()
            owner._replace(width, height, updated)
            return
        owner._w, owner._h = width, height
        owner._reset_next = True
        owner._notified_guidance_session = None
    else:
        worker_settings = dict(updated, host_backend=owner.backend)
        owner._session.request('update', settings=worker_settings)
    owner.settings = updated
    owner.preference = str(updated.get('host_backend', owner.preference))
    owner._sync_metadata()
