"""Disposable GPU export owner with bounded request waits and shared CPU input.

GPU frames cross process boundaries only via validated D3D12 shared resources.
The parent never dereferences a CUDA pointer. Native/driver hangs are contained
by terminating this worker, not by trying to interrupt a CUDA call in a thread.
"""
import ctypes
import multiprocessing
from multiprocessing import shared_memory
import os
from pathlib import Path
import tempfile
import threading
import time
import traceback

import numpy as np


class _KillJob:
    """Windows lifetime container also reaps FFmpeg descendants on cancellation."""
    def __init__(self,pid):
        from ctypes import wintypes as W
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',W.DWORD),
                ('min_working',ctypes.c_size_t),('max_working',ctypes.c_size_t),('active',W.DWORD),
                ('affinity',ctypes.c_size_t),('priority',W.DWORD),('scheduling',W.DWORD)]
        class Limits(ctypes.Structure):
            _fields_=[('basic',Basic),('io',ctypes.c_uint64*6),('process_memory',ctypes.c_size_t),
                ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        from dlss5tool.cuda_session import bind
        create=bind(kernel,'CreateJobObjectW',[ctypes.c_void_p,W.LPCWSTR],W.HANDLE)
        configure=bind(kernel,'SetInformationJobObject',[W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD])
        op=bind(kernel,'OpenProcess',[W.DWORD,W.BOOL,W.DWORD],W.HANDLE)
        assign=bind(kernel,'AssignProcessToJobObject',[W.HANDLE,W.HANDLE])
        self._close=bind(kernel,'CloseHandle',[W.HANDLE]);self.handle=create(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        process=None
        try:
            limits=Limits();limits.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not configure(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process=op(0x100|0x1,False,pid)  # SET_QUOTA | TERMINATE
            if not process or not assign(self.handle,process):raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:self.close();raise
        finally:
            if process:self._close(process)

    def close(self):
        if self.handle:self._close(self.handle);self.handle=None


def _memory(name):
    try:return shared_memory.SharedMemory(name=name,track=False)
    except TypeError:return shared_memory.SharedMemory(name=name)


def _worker(connection,input_name,args,kwargs):
    writer=memory=None;imported={};next_id=0
    try:
        from dlss5tool.gpu_video_export import NativeGpuVideoWriter
        memory=_memory(input_name)
        writer=NativeGpuVideoWriter(*args,**kwargs)
        connection.send(dict(ok=True,metadata={name:getattr(writer,name) for name in
            ('width','height','output_width','output_height','fps','codec','is_hdr','uses_nvenc','encoder_name','adapter_luid')}))
        while True:
            request=connection.recv();operation=request['operation']
            if operation=='write':
                dtype=np.dtype(request['dtype'])
                shape=(writer.height,writer.width,4 if writer.is_hdr else 3)
                if dtype not in ((np.dtype('float16'),np.dtype('float32')) if writer.is_hdr else (np.dtype('uint8'),)):
                    raise ValueError('Invalid shared frame dtype')
                frame=np.ndarray(shape,dtype,buffer=memory.buf)
                writer.write(frame);response={}
            elif operation=='import':
                descriptor=request['descriptor'];next_id+=1
                imported[next_id]=writer.import_shared_output(**descriptor)
                response=dict(lease=next_id,size=imported[next_id].size)
            elif operation=='device':
                identifier=request['lease']
                if identifier not in imported:raise ValueError('Unknown GPU lease')
                shared=imported[identifier]
                writer.write_device(shared.pointer.value,shared.size,request['layout'],writer.adapter_luid,
                    request['pitch'],strict_hdr=request['strict_hdr'])
                response={}
            elif operation=='release':
                shared=imported.pop(request['lease'],None)
                if shared:shared.close()
                response={}
            elif operation=='finish':
                writer.finish();connection.send(dict(ok=True,audio_mode=writer.audio_mode));break
            elif operation=='abort':
                writer.abort();connection.send(dict(ok=True));break
            else:raise ValueError('Unknown GPU export operation')
            connection.send(dict(ok=True,**response))
    except EOFError:pass
    except BaseException as error:
        try:connection.send(dict(ok=False,error=str(error),traceback=traceback.format_exc()[-5000:]))
        except (OSError,EOFError):pass
    finally:
        if writer:
            try:
                if not writer._finished:writer.abort()
            except Exception:pass
        if memory:memory.close()
        connection.close()


class _RemoteBuffer:
    def __init__(self,owner,identifier,size):
        self.owner=owner;self.pointer=ctypes.c_uint64(identifier);self.size=size;self._closed=False

    def close(self):
        if self._closed:return
        with self.owner._lock:
            if not self.owner._closed:self.owner._request(dict(operation='release',lease=self.pointer.value))
        self._closed=True


class ProcessGpuVideoWriter:
    def __init__(self,*args,cancel=None,command_timeout=120.,**kwargs):
        self._cancel=cancel;self._timeout=float(command_timeout);self._closed=False;self._failed=False
        self._lock=threading.Lock();self._memory=None;self._proc=None;self._connection=None
        self._transaction=None;self.cleanup_errors=[];self._job=None
        # Bound memory before spawning; constructor performs full contract check.
        width=args[1] if len(args)>1 else kwargs['width'];height=args[2] if len(args)>2 else kwargs['height']
        if type(width) is not int or type(height) is not int or width<1 or height<1 or width*height>3840*2160:
            raise ValueError('Positive dimensions up to 4K pixel budget required')
        try:
            self.output_path=Path(args[0] if args else kwargs['output_path']).resolve()
            if self.output_path.exists():raise FileExistsError(self.output_path)
            self._transaction=Path(tempfile.mkdtemp(prefix='.'+self.output_path.stem+'.gpu-',dir=self.output_path.parent))
            self._candidate=self._transaction/self.output_path.name
            if args:args=(str(self._candidate),*args[1:])
            else:kwargs={**kwargs,'output_path':str(self._candidate)}
            self._memory=shared_memory.SharedMemory(create=True,size=width*height*16)
            context=multiprocessing.get_context('spawn');self._connection,child=context.Pipe()
            self._proc=context.Process(target=_worker,args=(child,self._memory.name,args,kwargs),daemon=True)
            self._proc.start();child.close()
            self._job=_KillJob(self._proc.pid)
            reply=self._receive()
            for name,value in reply['metadata'].items():setattr(self,name,value)
        except BaseException:self._terminate();raise

    def _receive(self):
        deadline=time.monotonic()+self._timeout
        while True:
            if self._cancel is not None and self._cancel.is_set():
                self._terminate()
                from dlss5tool.frame_generation import Cancelled
                raise Cancelled('已取消GPU导出')
            if self._connection.poll(.05):
                try:reply=self._connection.recv()
                except (EOFError,OSError) as error:
                    self._terminate();raise RuntimeError('GPU export worker exited') from error
                if not reply.get('ok'):
                    self._terminate();raise RuntimeError('GPU export worker: '+reply.get('error','unknown'))
                return reply
            if not self._proc.is_alive() or time.monotonic()>=deadline:
                self._terminate();raise RuntimeError('GPU export worker exited or timed out; job stopped without fallback')

    def _request(self,request):
        if self._closed:raise RuntimeError('GPU export worker is closed')
        try:self._connection.send(request)
        except (OSError,EOFError) as error:
            self._terminate();raise RuntimeError('GPU export worker connection closed') from error
        return self._receive()

    def write(self,frame):
        with self._lock:
            if self._closed:raise RuntimeError('GPU writer closed')
            shape=(self.height,self.width,4 if self.is_hdr else 3)
            types=(np.float16,np.float32) if self.is_hdr else (np.uint8,)
            if frame.shape!=shape or frame.dtype not in types:raise ValueError('Wrong frame shape/dtype')
            np.copyto(np.ndarray(shape,frame.dtype,buffer=self._memory.buf),frame)
            self._request(dict(operation='write',dtype=frame.dtype.str))

    def import_shared_output(self,**descriptor):
        with self._lock:
            if descriptor['producer_luid']!=self.adapter_luid:raise ValueError('Physical GPU mismatch')
            reply=self._request(dict(operation='import',descriptor=descriptor))
            return _RemoteBuffer(self,reply['lease'],reply['size'])

    def write_device(self,pointer,nbytes,layout,adapter_luid,row_pitch=None,*,strict_hdr=True):
        with self._lock:
            if adapter_luid!=self.adapter_luid:raise ValueError('Physical GPU mismatch')
            self._request(dict(operation='device',lease=pointer,layout=layout,pitch=row_pitch,strict_hdr=strict_hdr))

    def _terminate(self):
        self._failed=True
        if self._proc is not None:
            if self._proc.is_alive():self._proc.terminate()
            self._proc.join(timeout=3)
            if self._proc.is_alive():self._proc.kill();self._proc.join(timeout=3)
        if self._job:self._job.close();self._job=None
        self._cleanup()

    def _cleanup(self):
        self._closed=True
        if self._job:self._job.close();self._job=None
        if self._connection:self._connection.close();self._connection=None
        if self._memory:
            self._memory.close();self._memory.unlink();self._memory=None
        if self._transaction:
            # This unique directory was created by this object; native writers
            # only create regular files here. Never follow links or recurse into
            # an unexpected directory, including after a driver/process crash.
            for path in self._transaction.iterdir():
                if path.is_symlink() or not path.is_file():
                    self.cleanup_errors.append(str(path));continue
                try:path.unlink()
                except OSError as error:self.cleanup_errors.append(str(error))
            try:self._transaction.rmdir()
            except OSError as error:self.cleanup_errors.append(str(error))
            self._transaction=None

    def finish(self):
        with self._lock:
            if self._closed:
                if self._failed:raise RuntimeError('GPU export failed')
                return
            try:
                self.audio_mode=self._request(dict(operation='finish'))['audio_mode']
                self._proc.join(timeout=3)
                if self._proc.is_alive():self._proc.terminate();self._proc.join(timeout=3)
                os.rename(self._candidate,self.output_path)
                self._cleanup()
            except BaseException:self._terminate();raise

    def abort(self):
        with self._lock:
            if self._closed:return
            try:
                # Cancellation may already be set: bounded immediate termination
                # takes priority over waiting for a blocked driver cleanup.
                if self._cancel is None or not self._cancel.is_set():self._request(dict(operation='abort'))
            except (OSError,RuntimeError):pass
            finally:self._terminate()
