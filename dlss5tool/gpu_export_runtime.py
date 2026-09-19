"""Verified optional component discovery and pre-frame export routing.

Selection never changes user codec, quality, size or bit depth. Unsupported
contracts use the existing FFmpeg route *before* processing starts. A running
native export never falls back mid-stream.
"""
import hashlib
import json
import os
from pathlib import Path

from dlss5tool import paths

FILES={'native_library':'encoder.dll','sdr_ptx':'sdr-yuv.ptx','hdr_ptx':'hdr-yuv.ptx',
       'worker':'frame-worker.exe','host_library':'enhancement.dll'}
_cache={}


def load_components(root=None):
    root=Path(root) if root else paths.runtime_root()/'gpu-export'
    manifest=root/'manifest.json'
    if not manifest.is_file():return None
    data=json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('schema')!=1 or not data.get('enabled'):return None
    identity=[]
    for name in ('manifest.json',*FILES.values()):
        path=root/name
        if path.is_symlink() or not path.is_file():raise RuntimeError('GPU export component missing/linked: '+str(path))
        stat=path.stat();identity.append((str(path.resolve()),stat.st_size,stat.st_mtime_ns))
    from dlss5tool.video_export import find_ffmpeg
    ffmpeg=Path(find_ffmpeg()).resolve();stat=ffmpeg.stat()
    identity.append((str(ffmpeg),stat.st_size,stat.st_mtime_ns))
    key=tuple(identity)
    if key not in _cache:
        for name in FILES.values():
            digest=hashlib.sha256((root/name).read_bytes()).hexdigest()
            if digest!=data.get('files',{}).get(name):raise RuntimeError('GPU export component hash mismatch: '+name)
        actual=hashlib.sha256(ffmpeg.read_bytes()).hexdigest()
        if actual!=data.get('ffmpeg_sha256'):return None
        _cache.clear();_cache[key]={field:str((root/name).resolve()) for field,name in FILES.items()}
    return dict(_cache[key])


def eligible(width,height,*,use_nvenc=None,nvenc_preset='p5',hdr_metadata=None,
             rate_control='quality',quality_profile='high',output_size=None,codec='auto',**_):
    from dlss5tool.video_export import classify_color_info,resolve_encoder_size
    if os.name!='nt' or use_nvenc is False:return False
    ow,oh=resolve_encoder_size(width,height,output_size)
    if ow*oh>3840*2160 or output_size is not None and (ow,oh)!=(width,height):return False
    if rate_control!='quality' or nvenc_preset!='p5' or quality_profile not in ('high','balanced'):return False
    hdr=classify_color_info(hdr_metadata) if hdr_metadata else None
    if hdr and hdr['is_hdr']:
        return (min(width,height)>=16 and hdr['color_primaries']=='bt2020'
                and hdr['color_space']=='bt2020nc' and codec in ('auto','hevc'))
    return codec in ('auto','h264')


def create_video_writer(output_path,width,height,fps,*args,gpu_options=None,cancel=None,**kwargs):
    from dlss5tool.video_export import FFmpegVideoWriter
    # Positional legacy options stay on the original exact constructor path.
    if args or not eligible(width,height,**kwargs):
        return FFmpegVideoWriter(output_path,width,height,fps,*args,**kwargs)
    if gpu_options is None:
        from dlss5tool.video_export import classify_color_info
        metadata=kwargs.get('hdr_metadata')
        # Local A/B found CPU-origin SDR and small HDR slower with process
        # isolation. Do not enable a known regression for cached/preview frames.
        if width*height<1920*1080 or not metadata or not classify_color_info(metadata)['is_hdr']:
            return FFmpegVideoWriter(output_path,width,height,fps,**kwargs)
    components=gpu_options if gpu_options is not None else load_components()
    if not components:return FFmpegVideoWriter(output_path,width,height,fps,**kwargs)
    import importlib.util
    if importlib.util.find_spec('av') is None:
        if gpu_options is not None:raise RuntimeError('GPU export requires requirements-gpu-export.txt')
        return FFmpegVideoWriter(output_path,width,height,fps,**kwargs)
    # Keep the existing encoder capability/codec decision; an unavailable NVENC
    # device must not turn a formerly valid software export into a native error.
    from dlss5tool.video_export import select_video_encoder,find_ffmpeg,classify_color_info
    hdr=kwargs.get('hdr_metadata');is_hdr=bool(hdr and classify_color_info(hdr)['is_hdr'])
    _,nvenc=select_video_encoder(find_ffmpeg(),(width+1)//2*2,(height+1)//2*2,fps,is_hdr,
        kwargs.get('use_nvenc'),kwargs.get('nvenc_preset','p5'),kwargs.get('rate_control','quality'),
        kwargs.get('quality_profile','high'),kwargs.get('video_bitrate_mbps',20),codec=kwargs.get('codec','auto'))
    if not nvenc:return FFmpegVideoWriter(output_path,width,height,fps,**kwargs)
    from dlss5tool.gpu_export_process import ProcessGpuVideoWriter
    options={key:components[key] for key in ('native_library','sdr_ptx','hdr_ptx') if key in components}
    options['device_ordinal']=components.get('device_ordinal',0)
    return ProcessGpuVideoWriter(output_path,width,height,fps,cancel=cancel,**options,**kwargs)
