"""Package the workspace release without credentials, git internals or raw caches."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent.parent
SKIP = {'.git', '.cache', '__pycache__', 'screenshots', '_site'}
dest = ROOT.parent / 'Areazone-v2-ready.zip'
with ZipFile(dest, 'w', ZIP_DEFLATED) as archive:
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if path.is_file() and not (set(relative.parts) & SKIP) and not path.name.endswith('.tmp'):
            archive.write(path, relative.as_posix())
with ZipFile(dest) as archive:
    assert archive.testzip() is None
    assert '.github/workflows/update-data.yml' in archive.namelist()
print(dest)
