import multiprocessing
import os
import threading
import unittest
from collections import OrderedDict
import numpy as np
from shared_cache_budget import SharedCacheBudget
from guidance_cache import RawGuidanceCache


def lease_child(name,conn):
    pool=SharedCacheBudget(name)
    with pool.locked():pool.publish_locked(64)
    conn.send('ready')
    conn.recv()
    os._exit(0)  # exercise crash reclamation, deliberately no close()


class SharedBudgetTests(unittest.TestCase):
    def test_two_caches_share_without_fixed_partition(self):
        gui=SharedCacheBudget(limit_bytes=400)
        cache=RawGuidanceCache(1,pool=SharedCacheBudget(gui.name))
        try:
            cache.put('a',np.ones(75,np.float32))
            self.assertEqual(cache.bytes,300) # exceeds supplied standalone limit
            with gui.locked():gui.publish_locked(100)
            cache.put('b',np.ones(25,np.float32))
            self.assertLessEqual(gui.snapshot()['used_bytes'],400)
            with gui.locked():gui.publish_locked(0)
            cache.put('c',np.ones(75,np.float32))
            self.assertEqual(cache.bytes,400)
            gui.set_limit(80);cache.trim()
            self.assertLessEqual(gui.snapshot()['used_bytes'],80)
            cache.close();self.assertEqual(gui.snapshot()['used_bytes'],0)
        finally:cache.close();gui.close()

    def test_pending_frame_demand_and_reverse_borrow(self):
        gui=SharedCacheBudget(limit_bytes=256)
        cache=RawGuidanceCache(0,pool=SharedCacheBudget(gui.name))
        try:
            cache.put('a',np.ones(50,np.float32))
            with gui.locked():gui.publish_locked(0,192)
            cache.trim();self.assertLessEqual(cache.bytes,64)
            with gui.locked():gui.publish_locked(192)
            cache.put('b',np.ones(32,np.float32))
            self.assertEqual(cache.bytes,0)
            with gui.locked():
                self.assertEqual(gui.allowance_locked(respect_demand=True),128)
                gui.publish_locked(96)
            cache.put('b',np.ones(32,np.float32))
            self.assertEqual(cache.bytes,128)
            self.assertLessEqual(gui.snapshot()['used_bytes'],256)
        finally:cache.close();gui.close()

    def test_multiple_leases_same_pid_and_dead_worker(self):
        owner=SharedCacheBudget(limit_bytes=256)
        other=SharedCacheBudget(owner.name)
        ctx=multiprocessing.get_context('spawn');parent,child=ctx.Pipe()
        process=ctx.Process(target=lease_child,args=(owner.name,child))
        try:
            with other.locked():other.publish_locked(100)
            process.start();self.assertTrue(parent.poll(10));parent.recv()
            self.assertEqual(owner.snapshot()['used_bytes'],164)
            parent.send('exit');process.join(10)
            self.assertFalse(process.is_alive())
            self.assertEqual(owner.snapshot()['used_bytes'],100)
            other.close();self.assertEqual(owner.snapshot()['used_bytes'],0)
        finally:
            if process.is_alive():process.terminate();process.join()
            parent.close();child.close();other.close();owner.close()

    def test_gui_evicts_clears_and_returns_shared_usage(self):
        from gui import App
        app=App.__new__(App)
        app._cache_lock=threading.RLock();app._frame=0
        app.fps=24
        app._preview_runtime_settings={'preview_cache_mb':1}
        app._preview_cache_bytes=lambda:256
        app._source_size=lambda:(8,4)
        app._active_preview_size=(8,4)
        app._playback_preview_size=lambda:(8,4)
        app._source_frame_cache=OrderedDict();app._dlss_frame_cache=OrderedDict()
        app._source_cache_bytes=app._dlss_cache_bytes=0
        app._queued_preview_frames=set();app._live_cache=app._last_shown_dlss=None
        app._preview_processed_frames=0;app._preview_process_t0=None
        pool=app._ensure_shared_cache_pool()
        raw=RawGuidanceCache(0,SharedCacheBudget(pool.name))
        try:
            raw.put('a',np.ones(50,np.float32))
            app._source_cache_store(0,np.zeros((4,8,3),np.uint8))
            self.assertLessEqual(pool.snapshot()['used_bytes'],256)
            raw.trim()
            app._source_cache_store(0,np.zeros((4,8,3),np.uint8))
            app._cache_store(0,('settings',),np.zeros((4,8,3),np.uint8))
            self.assertEqual(app._source_cache_bytes+app._dlss_cache_bytes,192)
            self.assertLessEqual(pool.snapshot()['used_bytes'],256)
            app._cache_clear();self.assertEqual(pool.snapshot()['used_bytes'],raw.bytes)
            raw.close();self.assertEqual(pool.snapshot()['used_bytes'],0)
        finally:raw.close();pool.close()

    def test_invalid_attach_is_rejected(self):
        with self.assertRaises(ValueError):SharedCacheBudget('bad')

    def test_concurrent_cache_insertions_never_double_spend(self):
        owner=SharedCacheBudget(limit_bytes=8192)
        errors=[]
        def work(index):
            raw=RawGuidanceCache(0,SharedCacheBudget(owner.name))
            try:
                for i in range(40):
                    raw.put((index,i),np.ones(256,np.float32))
                    if owner.snapshot()['used_bytes']>8192:errors.append('over budget')
            except Exception as error:errors.append(str(error))
            finally:raw.close()
        threads=[threading.Thread(target=work,args=(i,)) for i in range(4)]
        try:
            for thread in threads:thread.start()
            for thread in threads:thread.join(10)
            self.assertTrue(all(not t.is_alive() for t in threads))
            self.assertEqual(errors,[])
            self.assertEqual(owner.snapshot()['used_bytes'],0)
        finally:owner.close()

    def test_source_clear_and_shrink_clear_pending_demand(self):
        owner=SharedCacheBudget(limit_bytes=256)
        raw=RawGuidanceCache(0,SharedCacheBudget(owner.name))
        try:
            raw.put('a',np.ones(50,np.float32))
            owner.invalidate_guidance();raw.trim()
            self.assertEqual(raw.bytes,0)
            with owner.locked():owner.publish_locked(256)
            raw.put('b',np.ones(50,np.float32))
            self.assertEqual(raw.pending_bytes,200)
            owner.set_limit(100);raw.trim()
            self.assertEqual(raw.pending_bytes,0)
        finally:raw.close();owner.close()


if __name__=='__main__':unittest.main()
