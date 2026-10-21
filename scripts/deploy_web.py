"""Deploy the built SPA (web/dist) to the dev frontend S3 website bucket.

The dev hosting mode is S3 static-website hosting (see infra/stacks/frontend.py,
hostMode=s3 default); prod/CloudFront would need `aws s3 cp` with an OAC, so
this script is intentionally dev-only.
"""
import mimetypes
from pathlib import Path

import boto3

REGION = "ap-south-1"
BUCKET = "ar-dev-web-942893240068"
DIST = Path(__file__).resolve().parents[1] / "web" / "dist"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".woff2": "font/woff2",
}


def main() -> None:
    s3 = boto3.client("s3", region_name=REGION)
    if not DIST.is_dir():
        raise SystemExit(f"dist missing: {DIST} — run `npm run build` in web/ first")
    uploaded = 0
    for path in DIST.rglob("*"):
        if not path.is_file():
            continue
        key = path.relative_to(DIST).as_posix()
        ctype = CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(key)[0] or "application/octet-stream"
        extra = {"ContentType": ctype}
        if key.endswith(".html"):
            extra["CacheControl"] = "no-cache"  # SPA entrypoint must never be cached stale
        s3.put_object(Bucket=BUCKET, Key=key, Body=path.read_bytes(), **extra)
        uploaded += 1
    print(f"Uploaded {uploaded} objects to s3://{BUCKET}/")


if __name__ == "__main__":
    main()
