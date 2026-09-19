"""Desktop queue interactions. Selection and presentation never mutate files."""
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk, font as tkfont, messagebox

from dlss5tool.i18n import tr
from dlss5tool.image_sequence import output_source


def bounded_columns(total, minimums, preferred=None):
    """Reserve all three columns; resize the filename column with the viewport."""
    total = max(3, int(total))
    left, middle, right = minimums
    if total < left + middle + right:
        # Last-resort protection for a physically smaller-than-supported window.
        left = max(1, int(total * left / sum(minimums)))
        right = max(1, int(total * right / sum(minimums)))
        return left, max(1, total - left - right), right
    wanted_left, wanted_right = preferred or (left, right)
    left = max(left, min(int(wanted_left), total - middle - right))
    right = max(right, min(int(wanted_right), total - middle - left))
    return left, total - left - right, right


def elide(text, width, measure, keep_extension=False):
    text = str(text)
    if measure(text) <= width:
        return text
    suffix = os.path.splitext(text)[1] if keep_extension else ''
    if measure('…' + suffix) > width:
        suffix = ''
    if measure('…') > width:
        return ''
    stem = text[:-len(suffix)] if suffix else text
    low, high = 0, len(stem)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(stem[:mid] + '…' + suffix) <= width:
            low = mid
        else:
            high = mid - 1
    return stem[:low] + '…' + suffix


class QueueList:
    def __init__(self, app):
        self.app, self.tree = app, app.queue_tree
        self.anchor = None
        self.drag = None
        self.resize = None
        self.preferred = None
        self.scroll_timer = self.layout_timer = self.hover_timer = None
        self.hover_row = None
        self.tip = self.menu = None
        self.widths = (76, 220, 80)
        self.font = tkfont.Font(root=self.tree, font='TkDefaultFont')
        self.minimums = (60, 100, 60)
        bindings = {
            '<Configure>': self.schedule_layout, '<<ThemeChanged>>': self.schedule_layout,
            '<ButtonPress-1>': self.press, '<B1-Motion>': self.motion,
            '<ButtonRelease-1>': self.release, '<Double-Button-1>': self.double_click,
            '<Button-3>': self.context, '<Shift-F10>': self.context_key,
            '<Delete>': lambda e: self.command('remove'),
            '<Control-a>': self.select_all, '<Control-A>': self.select_all,
            '<Control-c>': lambda e: self.command('copy'),
            '<Control-z>': lambda e: self.command('undo'),
            '<Return>': lambda e: self.command('preview'), '<Escape>': self.escape,
            '<Up>': lambda e: self.navigate(e, -1), '<Down>': lambda e: self.navigate(e, 1),
            '<Home>': lambda e: self.navigate(e, 'first'), '<End>': lambda e: self.navigate(e, 'last'),
            '<space>': self.space, '<Motion>': self.hover, '<Leave>': self.hide_tip,
            '<MouseWheel>': self.hide_tip, '<FocusOut>': self.blur,
            '<Unmap>': self.blur, '<Destroy>': self.destroy,
        }
        for event, callback in bindings.items():
            self.tree.bind(event, callback)
        # Tk selects the most specific binding; explicitly handle modified arrows.
        for modifier in ('Shift', 'Control', 'Control-Shift'):
            for key, direction in (('Up', -1), ('Down', 1), ('Home', 'first'), ('End', 'last')):
                self.tree.bind(f'<{modifier}-{key}>', lambda e, d=direction: self.navigate(e, d))
        self.schedule_layout()

    def schedule_layout(self, event=None):
        if self.layout_timer is None:
            self.layout_timer = self.tree.after_idle(self.layout)

    def layout(self):
        self.layout_timer = None
        self.hide_tip()
        style = ttk.Style(self.tree)
        self.font = tkfont.Font(root=self.tree, font=style.lookup('Treeview', 'font') or 'TkDefaultFont')
        heading_font = tkfont.Font(root=self.tree, font=style.lookup('Treeview.Heading', 'font') or 'TkDefaultFont')
        states = [tr('queue.' + key) for key in ('pending', 'running', 'completed', 'failed', 'cancelled', 'interrupted')]
        self.minimums = (max(heading_font.measure(tr('label.status')) + 20,
                             max(self.font.measure(s) for s in states) + 12),
                         self.font.measure('filename…png') + 16,
                         max(heading_font.measure(tr('label.progress')) + 20, self.font.measure('100%') + 16))
        self.widths = bounded_columns(self.tree.winfo_width() - 4, self.minimums, self.preferred)
        for name, width in zip(('state', 'source', 'progress'), self.widths):
            self.tree.column(name, width=width, minwidth=1, stretch=False)
        self.tree.xview_moveto(0)
        for job in getattr(self.app, '_queue_jobs', ()):
            if self.tree.exists(job.job_id):
                self.app._update_queue_job_row(job)

    def format_values(self, values):
        values = list(values)
        for index, width in zip((0, 1, 5), self.widths):
            text = str(values[index])
            if index == 5 and self.font.measure(text) > width - 12:
                text = text.split(' · ', 1)[0]
            values[index] = elide(text, max(0, width - 12), self.font.measure, index == 1)
        return tuple(values)

    def reset_columns(self):
        self.preferred = None
        self.layout()

    def _rows(self):
        return self.tree.get_children('')

    def _range(self, first, last):
        rows = self._rows()
        if first not in rows or last not in rows:
            return set()
        a, b = rows.index(first), rows.index(last)
        return set(rows[min(a, b):max(a, b) + 1])

    def _select(self, rows):
        wanted = set(rows)
        if wanted != set(self.tree.selection()):
            self.tree.selection_set(tuple(row for row in self._rows() if row in wanted))

    def press(self, event):
        self.blur()
        self.tree.focus_set()
        region = self.tree.identify_region(event.x, event.y)
        if region == 'separator':
            column = self.tree.identify_column(event.x)
            if column in ('#1', '#2'):
                self.resize = (column, event.x, self.widths)
            return 'break'
        if region == 'heading':
            return 'break'
        row = self.tree.identify_row(event.y)
        base = set(self.tree.selection())
        ctrl, shift = bool(event.state & 4), bool(event.state & 1)
        if row:
            if not shift or self.anchor not in self._rows():
                self.anchor = row
            selected = self._range(self.anchor, row) if shift else {row}
            self._select(base ^ selected if ctrl and not shift else base | selected if ctrl else selected)
            self.tree.focus(row)
        elif not ctrl:
            self._select(())
        self.drag = dict(start=row, anchor=self.anchor if shift and row else row,
                         base=base if ctrl else set(), ctrl=ctrl and not shift,
                         x=event.x, y=event.y, current_y=event.y, moved=False)
        try:
            self.tree.grab_set()
        except tk.TclError:
            pass
        return 'break'

    def _nearest(self, y):
        rows = self._rows()
        if not rows:
            return None
        row = self.tree.identify_row(max(1, min(y, self.tree.winfo_height() - 2)))
        if row:
            return row
        top = min(len(rows) - 1, int(round(self.tree.yview()[0] * len(rows))))
        return rows[top] if y < 40 else rows[-1]

    def _drag_select(self):
        drag = self.drag
        if not drag or not drag['moved']:
            return
        row = self._nearest(drag['current_y'])
        if row is None:
            return
        if not drag['anchor']:
            drag['anchor'] = self._nearest(drag['y'])
            self.anchor = drag['anchor']
        selected = self._range(drag['anchor'], row)
        self._select(drag['base'] ^ selected if drag['ctrl'] else drag['base'] | selected)
        self.tree.focus(row)

    def motion(self, event):
        if self.resize:
            column, start, widths = self.resize
            delta = event.x - start
            if column == '#1':
                left = max(self.minimums[0], min(widths[0] + delta, sum(widths[:2]) - self.minimums[1]))
                self.preferred = (left, widths[2])
            else:
                right = max(self.minimums[2], min(widths[2] - delta, sum(widths[1:]) - self.minimums[1]))
                self.preferred = (widths[0], right)
            self.schedule_layout()
            return 'break'
        if self.drag:
            self.drag['current_y'] = event.y
            self.drag['moved'] |= abs(event.y - self.drag['y']) + abs(event.x - self.drag['x']) >= 4
            self._drag_select()
            if self.scroll_timer is None:
                self.scroll_timer = self.tree.after(75, self._autoscroll)
        return 'break'

    def _autoscroll(self):
        self.scroll_timer = None
        if not self.drag or not self.drag['moved']:
            return
        y = self.drag['current_y']
        direction = -1 if y < 35 else 1 if y > self.tree.winfo_height() - 18 else 0
        if direction:
            self.tree.yview_scroll(direction, 'units')
            self._drag_select()
            self.scroll_timer = self.tree.after(75, self._autoscroll)

    def release(self, event=None):
        self.drag = self.resize = None
        self._cancel_timer('scroll_timer')
        try:
            if self.tree.grab_current() is self.tree:
                self.tree.grab_release()
        except tk.TclError:
            pass
        return 'break'

    def double_click(self, event):
        self.release()
        if self.tree.identify_region(event.x, event.y) in ('cell', 'tree'):
            return self.command('preview')
        return 'break'

    def select_all(self, event=None):
        self._select(self._rows())
        return 'break'

    def escape(self, event=None):
        self.blur()
        self._select(())
        return 'break'

    def navigate(self, event, direction):
        rows = self._rows()
        if not rows:
            return 'break'
        old = self.tree.focus()
        index = rows.index(old) if old in rows else 0
        target = 0 if direction == 'first' else len(rows) - 1 if direction == 'last' else max(0, min(index + direction, len(rows) - 1))
        row = rows[target]
        if event.state & 1:
            if self.anchor not in rows:
                self.anchor = old if old in rows else rows[0]
            selection = self._range(self.anchor, row)
            self._select(set(self.tree.selection()) | selection if event.state & 4 else selection)
        elif not event.state & 4:
            self.anchor = row
            self._select((row,))
        self.tree.focus(row)
        self.tree.see(row)
        return 'break'

    def space(self, event):
        row = self.tree.focus()
        if row:
            self.anchor = row
            self._select(set(self.tree.selection()) ^ {row} if event.state & 4 else {row})
        return 'break'

    def menu_actions(self, heading=False, empty=False):
        """Return action IDs, labels, accelerators and enabled states for testing."""
        if heading:
            return [('reset', tr('queue.reset_columns'), '', True)]
        selected = [] if empty else self.app._selected_queue_jobs()
        editable = self.app._queue_editable()
        if not selected:
            return [(key, tr(label), '', editable) for key, label in (
                ('add_files', 'action.add_files'), ('add_folder', 'action.add_folder'), ('add_sequence', 'sequence.import'))] + [
                ('all', tr('queue.select_all'), 'Ctrl+A', bool(self._rows())),
                ('undo', tr('queue.undo_remove'), 'Ctrl+Z', editable and bool(getattr(self.app, '_queue_removed', ()))),
                ('reset', tr('queue.reset_columns'), '', True)]
        count = len(selected)
        return [
            ('preview', tr('action.load_preview'), 'Enter', editable and count == 1),
            ('apply', tr('queue.apply_selected', count=count), '', editable and all(j.state != 'completed' for j in selected)),
            ('retry', tr('queue.retry_selected'), '', editable and any(j.state in {'failed', 'cancelled', 'interrupted'} for j in selected)),
            ('copy', tr('queue.copy_paths'), 'Ctrl+C', True),
            ('location', tr('queue.open_source_location'), '', count == 1),
            ('remove', tr('queue.remove_selected', count=count), 'Del', editable),
            ('undo', tr('queue.undo_remove'), 'Ctrl+Z', editable and bool(getattr(self.app, '_queue_removed', ()))),
        ]

    def command(self, key):
        self.blur()
        selected = self.app._selected_queue_jobs()
        if key == 'reset':
            self.reset_columns()
        elif key == 'all':
            self.select_all()
        elif key == 'copy' and selected:
            try:
                paths = '\n'.join(output_source(j.source_path) for j in selected)
                self.tree.clipboard_clear()
                self.tree.clipboard_append(paths)
            except (OSError, ValueError) as error:
                messagebox.showerror(tr('queue.copy_paths'), str(error), parent=self.tree)
        elif key == 'location' and len(selected) == 1:
            try:
                directory = Path(output_source(selected[0].source_path)).parent
                if not directory.is_dir():
                    raise FileNotFoundError(str(directory))
                os.startfile(str(directory))
            except (OSError, ValueError) as error:
                messagebox.showerror(tr('queue.open_source_location'), str(error), parent=self.tree)
        elif self.app._queue_editable():
            callbacks = {'remove': self.app.remove_selected_queue_jobs, 'undo': self.app.undo_queue_remove,
                'preview': self.app.load_selected_queue_job, 'apply': self.app.apply_current_settings_to_queue,
                'retry': self.app.retry_selected_queue_jobs, 'add_files': self.app.add_queue_files,
                'add_folder': self.app.add_queue_folder, 'add_sequence': self.app.add_queue_image_sequence}
            if key == 'preview' and len(selected) != 1:
                return 'break'
            if key == 'apply' and any(j.state == 'completed' for j in selected):
                return 'break'
            callback = callbacks.get(key)
            if callback:
                callback()
        return 'break'

    def context(self, event):
        self.blur()
        self.tree.focus_set()
        heading = self.tree.identify_region(event.x, event.y) in ('heading', 'separator')
        row = self.tree.identify_row(event.y) if not heading else ''
        if row:
            if row not in self.tree.selection():
                self._select((row,))
                self.anchor = row
            self.tree.focus(row)
        elif not heading:
            self._select(())
        return self._popup(event.x_root, event.y_root, heading, not row)

    def context_key(self, event=None):
        self.blur()
        row = self.tree.focus()
        if not row and self.tree.selection():
            row = self.tree.selection()[0]
        box = self.tree.bbox(row) if row else ()
        x, y = (box[0] + 12, box[1] + box[3]) if box else (12, 32)
        return self._popup(self.tree.winfo_rootx() + x, self.tree.winfo_rooty() + y)

    def _popup(self, x, y, heading=False, empty=False):
        if self.menu:
            self.menu.destroy()
        ui = self.app._ui
        self.menu = tk.Menu(self.tree, tearoff=False, background=ui['panel'], foreground=ui['text'],
            activebackground=ui['select_bg'], activeforeground=ui['text'], disabledforeground=ui['muted'])
        for key, label, accelerator, enabled in self.menu_actions(heading, empty):
            if key in ('copy', 'remove', 'undo', 'all'):
                self.menu.add_separator()
            self.menu.add_command(label=label, accelerator=accelerator,
                state='normal' if enabled else 'disabled', command=lambda k=key: self.command(k))
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()
        return 'break'

    def hover(self, event):
        if self.drag or self.resize:
            return
        row = self.tree.identify_row(event.y)
        if row == self.hover_row:
            return
        self.hide_tip()
        self.hover_row = row
        if row:
            self.hover_timer = self.tree.after(500, lambda: self._show_tip(row, event.x_root, event.y_root))

    def _show_tip(self, row, x, y):
        self.hover_timer = None
        job = self.app._queue_job(row)
        if not job or self.hover_row != row:
            return
        ui = self.app._ui
        self.tip = tk.Toplevel(self.tree)
        self.tip.withdraw()
        self.tip.overrideredirect(True)
        try:
            source = output_source(job.source_path)
        except (OSError, ValueError):
            source = job.source_path
        text = tr('queue.paths', source=source, output=job.output_path or '')
        text += '\n' + self.app._queue_progress_text(job)
        tk.Label(self.tip, text=text, wraplength=440, justify='left', padx=8, pady=6,
            background=ui['tooltip_bg'], foreground=ui['tooltip_fg'], font=self.font).pack()
        self.tip.update_idletasks()
        x = max(0, min(x + 12, self.tree.winfo_screenwidth() - self.tip.winfo_reqwidth() - 8))
        y = max(0, min(y + 18, self.tree.winfo_screenheight() - self.tip.winfo_reqheight() - 8))
        self.tip.geometry(f'+{x}+{y}')
        self.tip.deiconify()

    def _cancel_timer(self, name):
        token = getattr(self, name)
        if token:
            self.tree.after_cancel(token)
            setattr(self, name, None)

    def hide_tip(self, event=None):
        self._cancel_timer('hover_timer')
        self.hover_row = None
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def blur(self, event=None):
        self.release()
        self.hide_tip()

    def destroy(self, event=None):
        if event is None or event.widget is self.tree:
            self.blur()
            self._cancel_timer('layout_timer')
            if self.menu:
                self.menu.destroy()
                self.menu = None
