"""Read-only source inventory for the bounded encoding acceptance task."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool.video_export import find_ffmpeg, find_ffprobe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    if not (args.work / 'TASK.md').is_file():
        parser.error('Registered task required')
    output = args.work / 'media-inventory.json'
    if output.exists():
        parser.error('Refusing to overwrite inventory')
    probe = find_ffprobe(find_ffmpeg())
    if not probe:
        parser.error('ffprobe not available')
    records = []
    for source in sorted(args.source.iterdir()):
        if not source.is_file() or source.suffix.lower() not in ('.mp4', '.mkv', '.mov', '.webm'):
            continue
        result = subprocess.run([probe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)],
                                capture_output=True, timeout=20,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        selected = []
        keys = ('index', 'codec_type', 'codec_name', 'profile', 'width', 'height', 'pix_fmt',
                'color_range', 'color_space', 'color_transfer', 'color_primaries',
                'r_frame_rate', 'avg_frame_rate', 'time_base', 'start_time', 'duration',
                'nb_frames', 'sample_rate', 'channels', 'side_data_list')
        for stream in data.get('streams', []):
            selected.append({key: stream[key] for key in keys if key in stream})
        records.append(dict(path=str(source.resolve()), size=source.stat().st_size,
                            returncode=result.returncode, streams=selected,
                            error=result.stderr.decode(errors='replace') if result.returncode else None))
    report = dict(scope='ffprobe metadata only, no decode or GPU use',
                  script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), files=records)
    with output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    for record in records:
        print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    main()
