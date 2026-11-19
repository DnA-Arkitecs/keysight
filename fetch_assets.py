"""Download the MediaPipe hand-tracking files once so the HUD runs from this folder.

After this runs, the HUD never needs the internet to track hands. Safe to re-run:
files already on disk are skipped.
"""
import sys
import urllib.request
from pathlib import Path

VERSION = "1.2.3"
NPM = f"https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{VERSION}"
MODEL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
         "hand_landmarker/float16/1/hand_landmarker.task")
VENDOR = Path(__file__).parent / "static" / "vendor"

FILES = {
    "vision_bundle.mjs": f"{NPM}/vision_bundle.mjs",
    "wasm/vision_wasm_internal.js": f"{NPM}/wasm/vision_wasm_internal.js",
    "wasm/vision_wasm_internal.wasm": f"{NPM}/wasm/vision_wasm_internal.wasm",
    "wasm/vision_wasm_module_internal.js": f"{NPM}/wasm/vision_wasm_module_internal.js",
    "wasm/vision_wasm_module_internal.wasm": f"{NPM}/wasm/vision_wasm_module_internal.wasm",
    "hand_landmarker.task": MODEL,
}


def main():
    for name, url in FILES.items():
        dest = VENDOR / name
        if dest.exists() and dest.stat().st_size > 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {name} ...")
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    print("MediaPipe files ready in static/vendor")


if __name__ == "__main__":
    sys.exit(main())
