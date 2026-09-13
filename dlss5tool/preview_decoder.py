"""Bounded background preview reader. Owns its capture; never calls Tk or app caches."""
from collections import OrderedDict
import threading

import cv2
from dlss5tool.video_export import tone_map_hdr_preview


class PreviewDecoder:
    def __init__(self, capacity=4):
        self.capacity = max(1, int(capacity))
        self._condition = threading.Condition()
        self._generation = 0
        self._source = None
        self._color = {}
        self._request = None
        self._active = None
        self._frames = OrderedDict()
        self._error = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name='preview-decode')
        self._thread.start()

    def invalidate(self):
        with self._condition:
            self._generation += 1
            self._source = self._request = self._active = None
            self._frames.clear()
            self._error = None
            self._condition.notify_all()

    def get(self, source, frame, color_info):
        frame = int(frame)
        with self._condition:
            if self._closed:
                return None
            color = dict(color_info or {})
            if source != self._source or color != self._color:
                self._generation += 1
                self._source, self._color = source, color
                self._frames.clear()
                self._request = self._active = None
                self._error = None
            if self._error:
                raise RuntimeError(self._error)
            if frame in self._frames:
                return self._frames.pop(frame)
            request = (self._generation, source, frame, color)
            if self._active is None or not (self._active[0] == self._generation and
                    self._active[2] <= frame < self._active[2] + self.capacity):
                self._request = request
                self._condition.notify_all()
            return None

    def close(self):
        with self._condition:
            self._closed = True
            self._frames.clear()
            self._condition.notify_all()
        # A codec read may be in progress. Only its owning thread releases capture.

    def _run(self):
        capture, opened, next_frame = None, None, None
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._request is not None or
                                              (capture is not None and self._source is None))
                    if self._closed:
                        return
                    if self._source is None:
                        capture.release()
                        capture, opened, next_frame = None, None, None
                        continue
                    request = self._request
                    self._request = None
                    self._active = request
                generation, source, first, color = request
                try:
                    if opened != source:
                        if capture is not None:
                            capture.release()
                        capture = cv2.VideoCapture(source)
                        if not capture.isOpened():
                            raise RuntimeError('Cannot open preview video')
                        opened, next_frame = source, 0
                    if next_frame != first:
                        capture.set(cv2.CAP_PROP_POS_FRAMES, first)
                    for index in range(first, first + self.capacity):
                        with self._condition:
                            if self._closed or generation != self._generation or self._request is not None:
                                break
                        ok, image = capture.read()
                        next_frame = index + 1
                        if not ok:
                            if index == first:
                                raise RuntimeError(f'Cannot decode preview frame {index}')
                            break
                        image = tone_map_hdr_preview(image, color)
                        with self._condition:
                            if self._closed or generation != self._generation:
                                break
                            self._frames[index] = image
                            while len(self._frames) > self.capacity:
                                self._frames.popitem(last=False)
                except Exception as exc:
                    with self._condition:
                        if generation == self._generation:
                            self._error = str(exc)
                finally:
                    with self._condition:
                        if self._active == request:
                            self._active = None
        finally:
            if capture is not None:
                capture.release()
