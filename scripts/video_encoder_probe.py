"""Exercise real 8K encoding without running the VSR/DLSS inference pipeline.

Run with the project's Python; add --cpu to validate the software fallback.
Temporary videos are removed after bitstream and decoded-frame validation.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dlss5tool.video_export import FFmpegVideoWriter, find_ffprobe, _CREATE_NO_WINDOW


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--width", type=int, default=7680)
    parser.add_argument("--height", type=int, default=4320)
    parser.add_argument("--preset", default="p7")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="dlss-encoder-probe-") as directory:
        output = str(Path(directory) / "8k-sdr.mp4")
        writer = FFmpegVideoWriter(
            output, args.width, args.height, 30,
            use_nvenc=False if args.cpu else None, nvenc_preset=args.preset,
        )
        try:
            frame = np.full((args.height, args.width, 3), 96, np.uint8)
            frame[:, :args.width // 2] = (32, 96, 160)
            writer.write(frame)
            writer.write(frame)
            writer.finish()
            ffprobe = find_ffprobe(writer.ffmpeg)
            if not ffprobe:
                raise RuntimeError("ffprobe required for bitstream validation")
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", output],
                capture_output=True, check=True, timeout=20, creationflags=_CREATE_NO_WINDOW,
            )
            stream = json.loads(result.stdout)["streams"][0]
            assert stream["codec_name"] == writer.codec
            assert (stream["width"], stream["height"]) == (writer.output_width, writer.output_height)
            assert stream["pix_fmt"] == "yuv420p"
            assert stream.get("color_transfer") not in ("smpte2084", "arib-std-b67")
            assert int(stream["nb_frames"]) == 2
            capture = cv2.VideoCapture(output)
            try:
                for _ in range(2):
                    ok, decoded = capture.read()
                    assert ok and decoded.shape == (writer.output_height, writer.output_width, 3)
                assert not capture.read()[0]
            finally:
                capture.release()
            print(json.dumps({
                "encoder": writer.encoder_name, "codec": stream["codec_name"],
                "size": [stream["width"], stream["height"]],
                "pixel_format": stream["pix_fmt"], "decoded_frames": 2,
                "bytes": Path(output).stat().st_size,
            }, ensure_ascii=False))
        finally:
            writer.abort()


if __name__ == "__main__":
    main()
