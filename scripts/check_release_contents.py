"""Reject repository-only developer material before archiving the main release."""
import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
USER_DOCUMENTS = {
    'readme.md', 'readme.en.md', 'changelog.md', 'third_party_notices.md',
    'guidance_parameters.md',
}
DEVELOPER_ROOTS = {
    'scripts', 'tests', 'native_amd_probe', 'amd_backend', 'third_party',
    'output', 'results', 'tmp',
}


def forbidden_contents(release_dir):
    release_dir = Path(release_dir)
    if not (release_dir / 'DLSS5Tool.exe').is_file():
        raise ValueError('Expected a main release directory containing DLSS5Tool.exe')
    developer_documents = {
        path.name.lower() for path in ROOT.glob('*.md')
    } - USER_DOCUMENTS
    developer_stems = {
        path.stem.lower() for path in (ROOT / 'scripts').glob('*.py')
    } | {'amd_devtest', 'amd_devtest_ui', 'build_amd_devtest', 'amd_probe', 'guidance_worker'}
    rejected = []
    for path in release_dir.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(release_dir)
        parts = tuple(part.lower() for part in relative.parts)
        # The base package may ship its mods instructions, never user components.
        if parts[0] == 'mods' and parts != ('mods', 'readme.md'):
            rejected.append(relative.as_posix())
            continue
        payload = parts[1:] if parts[0] == '_internal' else parts
        if (payload[0] in DEVELOPER_ROOTS
                or path.name.lower() in developer_documents
                or path.stem.lower() in developer_stems
                or path.name.lower().startswith('amd-devtest')
                or path.suffix.lower() == '.spec'):
            rejected.append(relative.as_posix())
    return sorted(rejected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release_dir', type=Path)
    args = parser.parse_args()
    try:
        rejected = forbidden_contents(args.release_dir)
    except ValueError as error:
        parser.exit(1, str(error) + '\n')
    if rejected:
        parser.exit(1, 'Refusing to archive development material:\n' + '\n'.join(rejected) + '\n')
    print('Release content isolation passed (AMD tools and experiments excluded).')


if __name__ == '__main__':
    main()
