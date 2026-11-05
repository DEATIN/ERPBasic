"""Собрать проверяемый архив демонстрации и контрольную сумму."""
import argparse
import hashlib
from pathlib import Path
import re
import zipfile


def assemble(version, destination):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', version):
        raise ValueError('Версия содержит недопустимые символы.')
    root = Path(__file__).resolve().parents[1]
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    directories = ['dist', 'tests', 'инструменты', 'документация', 'материалы', '.github', '.devcontainer']
    names = ['README.md', 'CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'SECURITY.md', 'CITATION.cff', 'LICENSE', 'package.json', '.editorconfig', '.gitattributes', '.gitignore', '.nojekyll']
    files = [root / name for name in names if (root / name).is_file()]
    for directory in directories:
        files.extend(path for path in (root / directory).rglob('*') if path.is_file() and '__pycache__' not in path.parts)
    archive = destination / f'ERPBasic-{version}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(set(files)):
            if path.is_symlink():
                raise ValueError('Символические ссылки в выпуск не включаются: ' + str(path.relative_to(root)))
            name = Path(f'ERPBasic-{version}') / path.relative_to(root)
            info = zipfile.ZipInfo(name.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, path.read_bytes())
    checksum = archive.with_suffix('.zip.sha256')
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
    return archive, checksum


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Собрать выпуск ERP Basic')
    parser.add_argument('--version', required=True, help='Версия выпуска, например v0.3.0')
    parser.add_argument('--output', default='artifacts', help='Каталог для архива')
    arguments = parser.parse_args()
    for result in assemble(arguments.version, arguments.output):
        print(result)
