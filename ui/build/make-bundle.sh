#!/bin/sh
# Build ui/bundle/: a relocatable Python 3.12 (python-build-standalone via uv),
# the project's Python packages (pip --target) and the adm core. electron-builder
# copies it into the .app as Contents/Resources/adm (extraResources), so the
# app runs without the project folder, uv, Homebrew or a venv.
set -e
cd "$(dirname "$0")/.."
ROOT="$(cd .. && pwd)"
OUT="$PWD/bundle"
command -v uv >/dev/null || { echo "need uv (brew install uv)"; exit 1; }

rm -rf "$OUT"; mkdir -p "$OUT"

echo "== python runtime"
PYHOME="$(uv python find --no-project 3.12 2>/dev/null || uv python find 3.12)"
PYHOME="$(cd "$(dirname "$(readlink -f "$PYHOME")")/.." && pwd)"
[ -x "$PYHOME/bin/python3.12" ] || { echo "python-build-standalone not found at $PYHOME (uv python install 3.12)"; exit 1; }
cp -R "$PYHOME" "$OUT/python"
# not needed at runtime: tests, IDLE, Tk, pip bootstrap
rm -rf "$OUT/python/lib/python3.12"/{test,idlelib,tkinter,ensurepip,lib2to3,__phello__} \
       "$OUT/python/lib"/{tcl9,tcl9.0,tk9.0,itcl4.3.8,thread3.0.6,libtcl9.0.dylib,libtcl9tk9.0.dylib,pkgconfig} \
       "$OUT/python/share" "$OUT/python/include" "$OUT/python/lib/python3.12/lib-dynload/_tkinter"*.so
find "$OUT/python" -name "__pycache__" -type d -prune -exec rm -rf {} +

echo "== packages"
uv pip install --python "$OUT/python/bin/python3" --target "$OUT/pylib" -r "$ROOT/requirements.txt" --quiet
find "$OUT/pylib" -name "__pycache__" -type d -prune -exec rm -rf {} +
rm -rf "$OUT/pylib/bin" "$OUT/pylib"/*.dist-info/RECORD

echo "== adm core"
cp -R "$ROOT/adm" "$OUT/adm"
rm -rf "$OUT/adm/__pycache__"
cp "$ROOT/requirements.txt" "$OUT/"
cat > "$OUT/run" <<'SH'
#!/bin/sh
# Entry point used by the packaged app: bundled interpreter + bundled packages.
DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$DIR:$DIR/pylib"
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
exec "$DIR/python/bin/python3" -m adm.cli "$@"
SH
chmod +x "$OUT/run"

echo "== smoke"
"$OUT/run" --json engine status >/dev/null
PYTHONPATH="$OUT:$OUT/pylib" "$OUT/python/bin/python3" -c "import camoufox, playwright, numpy; print('imports ok')"
du -sh "$OUT"
