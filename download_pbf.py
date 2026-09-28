"""
Download a fresh, uncorrupted copy of the North-Eastern Zone OSM PBF file
from Geofabrik. Uses binary mode to prevent text-mode corruption.
"""
import urllib.request
import os
import sys

URL = "https://download.geofabrik.de/asia/india/north-eastern-zone-latest.osm.pbf"
OUTPUT = r"C:\Users\jeet raj\OneDrive\Desktop\prediction hazard\data\north-eastern-zone-latest.osm.pbf"

def download():
    print(f"Downloading from: {URL}")
    print(f"Saving to: {OUTPUT}")
    print("This is ~190 MB, it may take a few minutes...")
    print()

    def progress(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            pct = min(100.0, downloaded * 100.0 / total_size)
            mb_done = downloaded / 1024 / 1024
            mb_total = total_size / 1024 / 1024
            sys.stdout.write(f"\r  Progress: {pct:5.1f}%  ({mb_done:.1f} / {mb_total:.1f} MB)")
            sys.stdout.flush()

    urllib.request.urlretrieve(URL, OUTPUT, reporthook=progress)

    size = os.path.getsize(OUTPUT)
    print(f"\n\n  Download complete: {size / 1024 / 1024:.1f} MB")

    # Verify file integrity
    with open(OUTPUT, "rb") as f:
        header = f.read(20)

    # Check for corruption markers
    efbfbd_count = header.count(b'\xef\xbf\xbd')
    if efbfbd_count > 0:
        print("  WARNING: File appears corrupted!")
    else:
        print("  File integrity check: OK")

    # Check OSM header
    if b'OSMHeader' in header:
        print("  OSM PBF header: OK")
    else:
        print("  WARNING: OSM PBF header not found!")

    print(f"\n  You can now run:")
    print(f"    python hazard_prediction_engine.py --pbf \"{OUTPUT}\"")

if __name__ == "__main__":
    download()
