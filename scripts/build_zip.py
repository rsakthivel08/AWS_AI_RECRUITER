"""Build zip package for Lambda functions."""
import os
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ZIP_PATH = BASE_DIR / "lambda_api_patch.zip"

def build():
    print(f"Building {ZIP_PATH}...")
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in ["src", "prompts", "data"]:
            folder_path = BASE_DIR / folder
            if not folder_path.exists():
                continue
            for root, dirs, files in os.walk(folder_path):
                dirs[:] = [d for d in dirs if d != "__pycache__" and not d.endswith(".egg-info")]
                for file in files:
                    if file.endswith((".pyc", ".pyo")):
                        continue
                    full_path = Path(root) / file
                    arcname = full_path.relative_to(BASE_DIR)
                    zf.write(full_path, arcname)
    print(f"Zip created successfully. Size: {ZIP_PATH.stat().st_size / (1024*1024):.2f} MB")

if __name__ == "__main__":
    build()
