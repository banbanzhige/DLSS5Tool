"""Timestamp-preserving packet muxing; never re-encodes video payloads.

The first access unit is parsed once to obtain codec parameters. Audio uses the
same FFmpeg policy as the existing writer. Only an explicitly selected GPU writer
imports this module's optional PyAV dependency.
"""
from fractions import Fraction
import io
import os
from pathlib import Path
import tempfile

from dlss5tool.nvenc_yuv import EncodedYuvPacket, YuvEncoderConfig


class PacketVideoWriter:
    def __init__(self, output_path, config, headers, *, audio_source=None):
        if not isinstance(config,YuvEncoderConfig):raise TypeError('YuvEncoderConfig required')
        self.output_path=Path(output_path).resolve()
        if self.output_path.suffix.lower() not in ('.mp4','.mkv','.mov'):
            raise ValueError('Only MP4/MKV/MOV are supported')
        if self.output_path.exists():raise FileExistsError(self.output_path)
        if not isinstance(headers,bytes) or not 0<len(headers)<=1024*1024:
            raise ValueError('Bounded codec headers required')
        self.config,self.headers=config,headers
        self.audio_source=audio_source
        self._container=None;self._stream=None;self._temp_path=None;self._audio_temp=None
        self._count=0;self._next_display=0;self._pending=set();self._last_dts=None
        self._closed=False;self._failed=False;self.audio_mode=None

    def _open(self, first):
        import av
        # Stream template copies decoder parameters, not an encoder context.
        # Explicit opaque=True prevents opening a software encoder on mux start.
        kind='hevc' if self.config.codec else 'h264'
        with av.open(io.BytesIO(self.headers+first.data),format=kind,mode='r') as parsed:
            template=parsed.streams.video[0]
            ctx=template.codec_context
            if (ctx.width,ctx.height)!=(self.config.width,self.config.height):
                raise ValueError('Bitstream dimensions differ from encoder contract')
            if self.config.codec and (ctx.color_primaries!=9 or ctx.colorspace!=9
                    or ctx.color_trc!=(16 if self.config.codec==1 else 18) or ctx.color_range!=1):
                raise ValueError('HDR bitstream tags differ from encoder contract')
            fd,path=tempfile.mkstemp(prefix='.'+self.output_path.stem+'.',
                suffix='.video.tmp'+self.output_path.suffix,dir=self.output_path.parent)
            os.close(fd);self._temp_path=path
            options={'avoid_negative_ts':'disabled'}
            if self.output_path.suffix.lower() in ('.mp4','.mov'):options['movflags']='+faststart'
            self._container=av.open(path,mode='w',options=options)
            self._stream=self._container.add_stream_from_template(template,opaque=True)
            self._stream.time_base=self.config.time_base
            self._stream.codec_context.framerate=Fraction(self.config.fps_num,self.config.fps_den)
            if self.config.codec and self.output_path.suffix.lower() in ('.mp4','.mov'):
                self._stream.codec_context.codec_tag='hvc1'

    def write(self, packet):
        if self._closed or self._failed:raise RuntimeError('Packet writer is closed or failed')
        if (not isinstance(packet,EncodedYuvPacket) or not packet.data
                or packet.time_base!=self.config.time_base or packet.duration!=1
                or type(packet.pts) is not int or type(packet.dts) is not int
                or packet.pts<self._next_display or packet.pts in self._pending
                or packet.pts>self._count+64 or packet.pts<packet.dts
                or self._last_dts is not None and packet.dts!=self._last_dts+1
                or self._count==0 and (not packet.is_keyframe or packet.pts!=0)):
            raise ValueError('Invalid CFR packet order, timestamps or first keyframe')
        try:
            import av
            if self._container is None:self._open(packet)
            # Sequence headers are container extradata, not duplicated per frame.
            target=av.Packet(packet.data)
            target.pts=packet.pts;target.dts=packet.dts;target.duration=packet.duration
            target.time_base=packet.time_base;target.is_keyframe=packet.is_keyframe
            target.stream=self._stream
            self._container.mux(target)
            self._last_dts=packet.dts;self._count+=1;self._pending.add(packet.pts)
            while self._next_display in self._pending:
                self._pending.remove(self._next_display);self._next_display+=1
        except BaseException:
            self._failed=True
            self.abort();raise

    def finish(self, expected_frames):
        if self._closed:
            if self._failed:raise RuntimeError('Packet writer failed or was aborted')
            return
        if (type(expected_frames) is not int or expected_frames<=0 or self._count!=expected_frames
                or self._next_display!=expected_frames or self._pending):
            self.abort();raise ValueError('Missing, duplicate or empty output timeline')
        try:
            self._container.close();self._container=None
            if self.output_path.exists():raise FileExistsError(self.output_path)
            if self.audio_source:
                from dlss5tool.video_export import mux_source_audio,find_ffmpeg
                # Publish only after the existing audio policy completes. Never
                # use -shortest: a short source audio track must not cut video.
                fd,self._audio_temp=tempfile.mkstemp(prefix='.'+self.output_path.stem+'.',
                    suffix='.audio.tmp'+self.output_path.suffix,dir=self.output_path.parent)
                os.close(fd)
                self.audio_mode=mux_source_audio(find_ffmpeg(),self._temp_path,
                    str(self.audio_source),self._audio_temp)
                os.rename(self._audio_temp,self.output_path);self._audio_temp=None
            else:
                # Windows rename refuses an existing destination.
                os.rename(self._temp_path,self.output_path);self._temp_path=None
                self.audio_mode='无音频源'
            self._closed=True
        except BaseException:
            self._failed=True;raise
        finally:
            self._remove_temp()

    def _remove_temp(self):
        for name in ('_temp_path','_audio_temp'):
            path=getattr(self,name,None)
            if path:
                try:os.unlink(path)
                except FileNotFoundError:pass
                setattr(self,name,None)

    def abort(self):
        self._failed=True;self._closed=True
        try:
            if self._container:self._container.close()
        finally:
            self._container=None;self._remove_temp()
