"""Preview-only comparison controls and exact-frame guidance inspection."""
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox

import cv2

from dlss5tool import ui_theme
from dlss5tool.i18n import tr
from dlss5tool.guidance_parameters import analysis_edge
from dlss5tool.guidance_public import depth_enabled, public_targets, normalize_public_settings
from dlss5tool.guidance_color import HDRAnalysisReader


def guidance_input_pair(source, frame, still, settings, color_info=None):
    """Decode an owned adjacent pair on a worker, never through Tk state."""
    capture = None
    hdr_reader = None
    try:
        if still is not None:
            original = still
        elif (color_info or {}).get('is_hdr'):
            hdr_reader = HDRAnalysisReader(source, color_info, start_frame=max(frame - 1, 0))
            original = hdr_reader.read()
            if original is None:
                raise RuntimeError(tr('status.frame_read_failed', frame=frame))
        else:
            capture = cv2.VideoCapture(source)
            capture.set(cv2.CAP_PROP_POS_FRAMES, max(frame - 1, 0))
            ok, original = capture.read()
            if not ok:
                raise RuntimeError(tr('status.frame_read_failed', frame=frame))
        height, width = original.shape[:2]
        scale = min(1.0, analysis_edge(settings) / max(width, height))
        size = (max(1, round(width * scale)), max(1, round(height * scale)))

        def rgba(image):
            return cv2.cvtColor(cv2.resize(image, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGBA)

        previous = None
        if still is None and frame > 0:
            previous = rgba(original)
            if hdr_reader is not None:
                original = hdr_reader.read()
            else:
                ok, original = capture.read()
                if not ok:
                    original = None
            if original is None:
                raise RuntimeError(tr('status.frame_read_failed', frame=frame))
        return rgba(original), previous
    finally:
        if capture is not None:
            capture.release()
        if hdr_reader is not None:
            hdr_reader.close()


class PreviewComparison:
    def _init_comparison(self):
        self._guidance_context = False
        self._normal_preview_view = self.view_var.get()
        saved = normalize_public_settings(getattr(self, '_saved_settings', {}))
        self._guidance_view = saved.get('guidance_preview_view', 'original')
        self.preview_selector = tk.StringVar(value=self.view_var.get())
        self.compare_layout = tk.StringVar(value=saved.get('preview_compare_layout', 'wipe'))
        self.compare_target = tk.StringVar(value=saved.get('guidance_compare_target', public_targets()[0]))
        self._guidance_result = None
        self._guidance_ready = None
        self._guidance_presented = None
        self._guidance_display_signature = None
        self._guidance_preview_queue = queue.SimpleQueue()
        self._guidance_preview_busy = False
        self._guidance_preview_after = None
        self._guidance_preview_epoch = 0
        self._guidance_display_time = None

    def _install_comparison_menu(self, view_bar):
        group = view_bar._group
        def click(event):
            if any(value == 'compare' and x0 <= event.x <= x1
                   for value, x0, x1 in group._hits):
                self._show_comparison_menu(view_bar)
        group.bind('<Button-1>', click, add='+')
        for key in ('<Down>', '<Return>', '<space>'):
            group.bind(key, lambda e, bar=view_bar: self._show_comparison_menu(bar))

    def _build_comparison_menu(self, parent):
        menu = tk.Menu(parent, tearoff=False, font=ui_theme.UI_FONT,
                       bg=self._ui['panel'], fg=self._ui['text'],
                       activebackground=self._ui['select_bg'], activeforeground=self._ui['text'])
        if self._guidance_context:
            for target in public_targets():
                menu.add_radiobutton(label=tr('compare.target', view=tr('view.' + target)),
                                     variable=self.compare_target, value=target,
                                     command=self._comparison_changed)
            menu.add_separator()
        for value in ('wipe', 'side'):
            menu.add_radiobutton(label=tr('compare.' + value), variable=self.compare_layout,
                                 value=value, command=self._comparison_changed)
        menu.add_command(label=tr('compare.center'), command=self._center_split,
                         state='normal' if self.compare_layout.get() == 'wipe' else 'disabled')
        if self._guidance_context:
            menu.add_separator()
            menu.add_command(label=tr('guidance.legend.title'), command=self._show_guidance_legend)
        return menu

    def _show_comparison_menu(self, view_bar):
        if self.preview_selector.get() != 'compare':
            return None
        previous = getattr(self, '_compare_popup', None)
        if previous is not None:
            previous.destroy()
        menu = self._build_comparison_menu(view_bar)
        self._compare_popup = menu
        try:
            menu.update_idletasks()
            menu.tk_popup(view_bar.winfo_rootx(), max(0, view_bar.winfo_rooty() - menu.winfo_reqheight()))
        finally:
            menu.grab_release()
        return 'break'

    def _show_guidance_legend(self):
        messagebox.showinfo(tr('guidance.legend.title'),
                            tr('guidance.legend.' + self.compare_target.get()),
                            parent=self.canvas.winfo_toplevel())

    def _active_compare(self):
        if getattr(self, '_guidance_context', False):
            return getattr(self, '_guidance_view', 'original') == 'compare'
        variable = getattr(self, 'view_var', None)
        return bool(variable is not None and variable.get() == 'compare')

    def _wipe_compare(self):
        layout = getattr(self, 'compare_layout', None)
        return self._active_compare() and (layout is None or layout.get() == 'wipe')

    def _compare_label(self):
        if getattr(self, '_guidance_context', False):
            return tr('view.' + self.compare_target.get())
        return 'DLSS'

    def _sync_comparison_controls(self):
        context = self._guidance_context
        choices = {'original': tr('view.original')}
        choices.update({name: tr('view.' + name) for name in public_targets()} if context else {'dlss': 'DLSS'})
        choices['compare'] = tr('compare.menu')
        for pane in (getattr(self, '_docked_preview_pane', None), getattr(self, '_detached_preview_pane', None)):
            if not pane:
                continue
            pane['view_bar'].set_choices(choices)

    def _on_workspace_selected(self, page):
        if not hasattr(self, '_guidance_page') or not hasattr(self, '_settings'):
            return
        context = page is self._guidance_page
        if context == self._guidance_context:
            return
        self.pause()
        self._freeze_preview_cache(resume_ms=None)
        self._guidance_preview_epoch += 1
        self._guidance_result = None
        self._guidance_ready = self._guidance_presented = None
        self._guidance_display_signature = None
        if context:
            self._normal_preview_view = self.view_var.get()
            self.view_var.set('original')  # ordinary DLSS prefetch is inactive here
        else:
            self.view_var.set(self._normal_preview_view)
        self._guidance_context = context
        self.preview_selector.set(self._guidance_view if context else self.view_var.get())
        self._split_frame = -1
        self._split_orig = self._split_dlss = None
        self._last_viewport_image = None
        self._sync_comparison_controls()
        self.on_view_change()

    def _on_preview_selection(self):
        selection = self.preview_selector.get()
        self._schedule_settings_save()
        self._guidance_display_signature = None
        if self._guidance_context:
            self._guidance_view = selection
            if selection in ('depth', 'flow'):
                self.compare_target.set(selection)
            self.pause()
            self._split_orig = self._split_dlss = None
            self._sync_comparison_controls()
            self.display_view()
        else:
            self.view_var.set(selection)
            self.on_view_change()

    def _comparison_changed(self):
        self._schedule_settings_save()
        self._sync_comparison_controls()
        self._refresh_viewport_display() if self.video else self._draw_empty()

    def _center_split(self):
        self.split_x = 0.5
        self._refresh_viewport_display()

    def _guidance_preview_key(self, frame=None):
        return (self.video, self._frame if frame is None else frame, self._guidance_generation,
                self._guidance_preview_epoch, self._settings_hash())

    def _guidance_unavailable(self):
        target = self.compare_target.get() if self._guidance_view == 'compare' else self._guidance_view
        if target == 'original':
            return ''
        if self._switching_backend:
            return tr('guidance.switching')
        mode = self._collect_host_settings()['guidance_mode']
        if mode not in ((2, 3) if target == 'depth' else (1, 3)):
            return tr('guidance.preview_disabled', view=tr('view.' + target))
        if getattr(self, '_is_image', False) and target == 'flow':
            return tr('guidance.still_hint')
        if self._switching_backend or self._queue_running or self._diagnosing:
            return tr('guidance.preview_busy')
        return ''

    def _set_guidance_status(self, text):
        # Do not flush Tk idles every playback tick: that can re-enter resize
        # handlers between updating the frame number and painting its image.
        if self.eta_label.cget('text') != text:
            self.eta_label.configure(text=text)

    def _display_guidance(self, frame, original=None):
        cw, ch = self._canvas_size()
        if self._switching_backend:
            self._draw_work_status(tr('guidance.switching'))
            return
        self._draw_work_status('')
        target = self.compare_target.get() if self._guidance_view == 'compare' else self._guidance_view
        if target == 'original' or self._hold_original:
            self._guidance_display_signature = None
            original = original if original is not None else self._read_frame(frame)
            if original is not None:
                self._dlss_pending = False
                self._draw_fit(original, cw, ch)
            return
        reason = self._guidance_unavailable()
        key = self._guidance_preview_key(frame)
        result = self._guidance_result
        self._dlss_pending = False
        if not reason and result and result[0] == key:
            _, original, images, reset, error = result
            if error:
                reason = error
            elif target in images:
                self._render_guidance_result(result, target, cw, ch)
                status = tr('guidance.preview_frame', frame=frame)
                if target == 'flow' and reset:
                    status += ' · ' + tr('guidance.flow_reset')
                metrics = images.get('_metrics', {})
                if metrics:
                    def size(name):
                        value = metrics.get(name + '_size')
                        return '×'.join(map(str, value)) if value else '—'
                    def timing(name):
                        if metrics.get('cache_' + name + '_hit'):
                            return tr('guidance.metrics.cached')
                        if name == 'flow' and reset:
                            return tr('guidance.metrics.reset')
                        return str(round(metrics.get(name + '_ms', 0), 1))
                    status += ' · ' + tr('guidance.nvofa_metrics' if metrics.get('flow_backend') == 'nvofa' else 'guidance.metrics' if depth_enabled() else 'guidance.flow_metrics', flow=size('flow'), depth=size('depth'),
                                         updates=metrics.get('flow_updates', 0),
                                         flow_ms=timing('flow'), depth_ms=timing('depth'))
                self._set_guidance_status(status)
                return
        if not reason:
            self._request_guidance_preview(key)
            # Retain an entire matched pair during a seek. Never combine a new
            # source frame with an old map, and never blank the video while waiting.
            held = self._guidance_presented
            if held and held[0][0] == key[0] and held[0][2:] == key[2:] and target in held[2]:
                self._render_guidance_result(held, target, cw, ch)
                self._set_guidance_status(tr('guidance.preview_waiting', frame=frame, shown=held[0][1]))
                self._draw_work_status(tr('guidance.preview_waiting', frame=frame, shown=held[0][1]))
                return
            signature = ('pending', key, id(self.canvas), cw, ch, self._preview_zoom,
                         self._preview_pan_x, self._preview_pan_y)
            if signature != self._guidance_display_signature:
                source = original if original is not None else self._read_frame(frame)
                if source is not None:
                    self._draw_fit(source, cw, ch)
                self._guidance_display_signature = signature
            self._split_orig = self._split_dlss = None
            self._set_guidance_status(tr('guidance.preview_pending', frame=frame))
            self._draw_work_status(tr('guidance.preview_pending', frame=frame))
            return
        # Unavailable/error states are distinct from ordinary frame buffering.
        self._split_orig = self._split_dlss = None
        self._last_viewport_image = None
        self._guidance_display_signature = None
        self.canvas.delete('all')
        self.canvas.create_text(cw / 2, ch / 2, text=reason, width=max(100, cw - 40),
                                fill=self._ui_color('text', '#eeeeee'), font=ui_theme.UI_FONT)

    def _render_guidance_result(self, result, target, cw, ch):
        key, original, images, reset, _error = result
        signature = (key, target, self._guidance_view, self.compare_layout.get(),
                     self._preview_zoom, self._preview_pan_x, self._preview_pan_y,
                     self.split_x, id(self.canvas), cw, ch, self._ui_theme_name)
        if signature == self._guidance_display_signature:
            return
        visual = images[target]
        if self._guidance_view == 'compare':
            self._split_orig, self._split_dlss = original, visual
            self._split_frame = key[1]
            self._blit_split(cw, ch)
        else:
            self._draw_fit(visual, cw, ch, badge=tr('view.' + target))
        self._guidance_presented = result
        self._guidance_display_signature = signature

    def _request_guidance_preview(self, key):
        if self._guidance_preview_busy or getattr(self, '_clear_preview_pending', False):
            return
        previous = getattr(self, '_play_dlss_thread', None)
        if previous is not None and previous.is_alive():
            if self._guidance_preview_after is None:
                self._guidance_preview_after = self.root.after(40, self._poll_guidance_preview)
            return
        settings = self._collect_settings()
        frame, source = key[1], key[0]
        still = self._image_bgr.copy() if self._is_image else None
        color_info = dict(self._video_color_info or {})
        self._guidance_preview_busy = True

        def work():
            try:
                current, previous_rgba = guidance_input_pair(source, frame, still, settings, color_info)
                size = (current.shape[1], current.shape[0])
                with self._live_lock:
                    if key[2:4] != (self._guidance_generation, self._guidance_preview_epoch):
                        raise RuntimeError('Retired guidance preview')
                    live = self._ensure_live(*size, settings=settings)
                    if live is None:
                        raise RuntimeError(self._live_error)
                    try:
                        images, reset = live.guidance_preview(current, previous_rgba)
                        images['_metrics'] = dict(getattr(live, 'guidance_metrics', {}))
                    finally:
                        self._last_dlss_frame = -1
                source_image = cv2.cvtColor(current, cv2.COLOR_RGBA2BGR)
                result = (key, source_image, images, reset, '')
            except Exception as error:
                result = (key, None, {}, False, str(error))
            self._guidance_preview_queue.put(result)

        # Existing retirement/export/close code waits for this same worker slot.
        thread = threading.Thread(target=work, name='guidance-preview', daemon=True)
        self._play_dlss_thread = thread
        thread.start()
        self._guidance_preview_after = self.root.after(40, self._poll_guidance_preview)

    def _poll_guidance_preview(self):
        self._guidance_preview_after = None
        try:
            result = self._guidance_preview_queue.get_nowait()
        except queue.Empty:
            if self._guidance_preview_busy:
                self._guidance_preview_after = self.root.after(40, self._poll_guidance_preview)
            elif self._guidance_context and self.video and not self._exporting:
                self.display_view()
            return
        self._guidance_preview_busy = False
        if self._guidance_context and self.video and not self._exporting:
            if result[0] == self._guidance_preview_key():
                self._guidance_result = result
                self._guidance_display_time = time.perf_counter()
                if result[-1] and self.playing:
                    self.pause()
                self.display_view()
            elif (self.playing and self._guidance_view != 'original'
                  and result[0] == self._guidance_preview_key(self._frame + 1)):
                # Back buffer only. The playback clock commits the frame number
                # and both images together; the front buffer stays visible.
                self._guidance_ready = result
            else:
                self.display_view()

    def _guidance_play_tick(self):
        self._audio.pause()  # inference-paced inspection, not audio-clock playback
        if self._guidance_unavailable():
            self.pause()
            self.display_view()
            return
        result = self._guidance_result
        if result and result[0] == self._guidance_preview_key():
            if result[-1]:
                self.pause()
                self.display_view()
                return
            now = time.perf_counter()
            if self._guidance_display_time is None:
                self._guidance_display_time = now
            elapsed = now - self._guidance_display_time >= 1 / max(self.fps, 1)
            if self._frame >= self._last_frame_index():
                if elapsed:
                    self.pause()
                    return
            else:
                next_key = self._guidance_preview_key(self._frame + 1)
                ready = self._guidance_ready
                if ready and ready[0] == next_key and elapsed:
                    self._guidance_ready = None
                    if ready[-1]:
                        self.pause()
                        self.set_status(ready[-1])
                        return
                    self._guidance_result = ready
                    self._frame = next_key[1]
                    self.timeline.set(self._frame)
                    self._sync_transport_labels()
                    self._guidance_display_time = now
                elif not ready or ready[0] != next_key:
                    self._request_guidance_preview(next_key)
        self._display_guidance(self._frame)
        self._play_after = self.root.after(30, self._play_tick)

    def _blit_side_by_side(self, cw, ch):
        from PIL import Image, ImageTk
        slot = max(1, (cw - 12) // 2)
        left, geom = self._render_viewport_image(self._split_orig, slot, ch)
        if left is None:
            return
        ox, oy, nw, nh = geom
        right = None
        if self._split_dlss is not None:
            right, _ = self._render_viewport_image(self._split_dlss, slot, ch, track=False)
            right = cv2.resize(right, (nw, nh))
        if right is None:
            right = self._pending_preview_image(left)
        self.canvas.delete('all')
        self._compare_photos = []
        for offset, image, label in ((0, left, tr('view.original')), (slot + 12, right, self._compare_label())):
            photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)))
            self._compare_photos.append(photo)
            self.canvas.create_image(offset + ox, oy, anchor='nw', image=photo)
            self._canvas_shadow_text(offset + ox + 10, oy + 14, label, anchor='w', font=ui_theme.UI_FONT_SMALL)
        self._navigator_geom = None
        self._draw_pending_status(cw, ch)
