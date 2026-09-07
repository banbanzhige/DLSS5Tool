import tkinter as tk
import unittest
from unittest.mock import patch

from ui_theme import tokens
from ui_widgets import TimelineBar


class TimelineBarTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.bar = TimelineBar(self.root)
        self.addCleanup(self.root.destroy)

    def test_cache_uses_translucent_progress_color_in_both_themes(self):
        def rgb(color):
            return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))

        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                self.bar.apply_theme(tokens(theme))
                colors = self.bar._colors
                track, fill = rgb(colors["track"]), rgb(colors["fill"])
                for state, opacity in (("rendered", 0.45), ("queued", 0.16)):
                    self.assertEqual(rgb(colors[state]), tuple(
                        round(a + (b - a) * opacity) for a, b in zip(track, fill)
                    ))
                self.assertEqual(len(set(colors[k] for k in
                                         ("track", "queued", "rendered", "fill"))), 4)

    def test_cache_is_inside_rail_below_playback_and_thumb(self):
        bar = self.bar
        with patch.object(bar, "winfo_width", return_value=640), \
                patch.object(bar, "winfo_height", return_value=24):
            bar.set_range(0, 100)
            bar.set(40)
            bar.set_cache_ranges([(0, 20), (40, 65), (80, 150)], [(55, 90)])
            track = bar.find_withtag("track")[0]
            left, y, right, _ = bar.coords(track)
            for state in ("queued", "rendered"):
                for item in bar.find_withtag(state):
                    x0, y0, x1, y1 = bar.coords(item)
                    self.assertEqual((y0, y1), (y, y))
                    self.assertGreaterEqual(x0, left)
                    self.assertLessEqual(x1, right)
                    self.assertEqual(bar.itemcget(item, "width"),
                                     bar.itemcget(track, "width"))
            self.assertEqual([bar.gettags(item)[0] for item in bar.find_all()],
                             ["track", "queued", "rendered", "rendered",
                              "rendered", "played", "thumb"])
            bar.set_cache_ranges()
            self.assertFalse(bar.find_withtag("rendered"))
            self.assertFalse(bar.find_withtag("queued"))
            self.assertEqual(bar.get(), 40)
            bar.set_range(0, 0)
            bar.set_cache_ranges([(0, 0)], [(0, 0)])
            self.assertEqual([bar.gettags(item)[0] for item in bar.find_all()], ["track"])


if __name__ == "__main__":
    unittest.main()
