"""Project-scoped Windows release paths; never write to the shared dist root."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import re

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'packaging/release-output.json'
LOCAL_POLICY = ROOT / 'packaging/release-output.local.json'
ENV_RELEASE_ROOT = 'DLSS5_RELEASE_ROOT'


def _load_policy(policy_path, local_policy_path):
    data = json.loads(Path(policy_path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ValueError('Unsupported release output policy')
    merged = dict(data)
    using_default = Path(policy_path).resolve() == POLICY.resolve()
    local_file = LOCAL_POLICY if (local_policy_path is None and using_default) else local_policy_path
    if local_file is not None and Path(local_file).is_file():
        local_data = json.loads(Path(local_file).read_text(encoding='utf-8'))
        if not isinstance(local_data, dict) or local_data.get('schema') != 1:
            raise ValueError('Unsupported local release output policy')
        for key in ('release_root', 'project_folder'):
            if key in local_data:
                merged[key] = local_data[key]
    if using_default and local_policy_path is None:
        env_root = os.environ.get(ENV_RELEASE_ROOT, '').strip()
        if env_root:
            merged['release_root'] = env_root
    return merged


def release_paths(version, *, policy_path=POLICY, local_policy_path=None, day=None):
    data = _load_policy(policy_path, local_policy_path)
    root = PureWindowsPath(data['release_root'])
    project = data['project_folder']
    if not root.is_absolute() or root == PureWindowsPath(root.anchor):
        raise ValueError('Release root must be a named absolute Windows directory')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', project):
        raise ValueError('One project folder is required under the shared release root')
    if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
        raise ValueError('Release version must be vX.Y.Z')
    day = day or datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')
    if not re.fullmatch(r'\d{8}', day):
        raise ValueError('Task date must be YYYYMMDD')
    project_root = root / project
    release = project_root / version
    return {key: value.as_posix() for key, value in {
        'project': project_root, 'release': release,
        'base': release / 'base', 'editions': release / 'editions',
        'work': project_root / 'tmp' / f'release-{version}-{day}',
    }.items()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(release_paths(args.version)))
