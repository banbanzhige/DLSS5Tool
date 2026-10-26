"""Read-only candidate pairing shared by bounded S4 acceptance scripts."""
from pathlib import Path


def components_for(task):
    from scripts.build_vsr_cuda import safe, digest, ROOT
    from dlss5tool.resident_sr_runtime import load_components
    from dlss5tool.cuda_vsr_runtime import load_components as load_vsr
    task = safe(task, ROOT/'tmp')
    result, pair = load_components(), load_vsr()
    if result is None or pair is None:
        raise ValueError('Existing verified S3 pair required')
    result.update(hdr_vsr_library=str(safe(task/'build/vsr_cuda.dll', task)),
        hdr_pack_ptx=str(safe(task/'vsr-hdr-v2.ptx', task)),
        p010_ptx=str(safe(task/'p010-rgba-v2.ptx', task)),
        device_host=str(safe(task/'consumer-build/enhancement.dll', task)),
        device_worker=str(safe(task/'consumer-build/frame-worker.exe', task)),
        analysis_ptx=str(safe(task/'analysis-v2.ptx', task)),
        analysis_tables=str(safe(task/'analysis-tables.npz', task)),
        cudart_directory=pair['cudart_directory'], runtime_directory=pair['runtime_directory'])
    keys = ('nv12_ptx', 'native_library', 'sdr_ptx', 'hdr_ptx', 'hdr_vsr_library',
            'hdr_pack_ptx', 'p010_ptx', 'device_host', 'device_worker', 'analysis_ptx', 'analysis_tables')
    result['component_hashes'] = {key:digest(safe(result[key])) for key in keys}
    from dlss5tool.analysis_quantization import cpu_fingerprint
    result['analysis_cpu'] = cpu_fingerprint()
    return result
