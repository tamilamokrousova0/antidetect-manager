"""`camoufox fetch` (browser ~1.3 GB, GeoIP, addons) with the parallel
downloader. Used by setup.sh and update_engine.py; the app uses `adm engine fetch`."""
from adm import engine

engine.fetch(engine.print_progress(as_json=False))
