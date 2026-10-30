#!/usr/bin/env python3
"""Convert images to palette PNGs, upload them to Cloudflare R2 and print their public URLs."""

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps

BUCKET = os.environ.get("R2_BUCKET", "my-bucket")
ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL", f"https://{BUCKET}.r2.cloudflarestorage.com")
BASE_URL = os.environ.get("R2_BASE_URL", "https://assets.example.com")

MAX_DOWNLOAD_BYTES = 50_000_000
DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_DIMENSION = 2048
PALETTE_COLORS = 256


def log(message):
    print(message, file=sys.stderr)


def is_url(value):
    return re.match(r"https?://", value) is not None


def download_image(url):
    """Return the bytes of the image at url, or raise if it is too large or not an image."""
    request = Request(url, headers={"User-Agent": "picbedmanager"})
    with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"file is larger than {MAX_DOWNLOAD_BYTES} bytes")
    Image.open(BytesIO(data)).verify()
    return data


def _to_rgb(image):
    """Return image as RGBA if it has transparency, otherwise as RGB."""
    if image.mode.startswith("I"):
        # 16-bit greyscale would clip to white if converted directly.
        image = image.point(lambda value: value * (1 / 256)).convert("L")
    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        return image.convert("RGBA")
    return image.convert("RGB")


def _encode_palette_png(image):
    if image.mode == "RGBA":
        paletted = image.quantize(PALETTE_COLORS, method=Image.Quantize.FASTOCTREE)
    else:
        palette = image.quantize(PALETTE_COLORS)
        paletted = image.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
    # Drops ICC profiles and other metadata carried over from the source.
    paletted.info.clear()
    output = BytesIO()
    paletted.save(output, "PNG", optimize=True)
    return output.getvalue()


def convert_image(source, crop_wide=True):
    """Return source (a path or binary file) as palette PNG bytes.

    Images wider than 2:1 are center-cropped to 2:1 when crop_wide is set, then
    anything larger than MAX_DIMENSION on either side is scaled down to fit.
    """
    with Image.open(source) as image:
        if image.format != "PNG":
            log("Converting to png...")

        # Metadata gets stripped, so apply the EXIF rotation to the pixels first.
        image = ImageOps.exif_transpose(image)

        width, height = image.size
        if crop_wide and width > height * 2:
            target_width = height * 2
            left = (width - target_width) // 2
            image = image.crop((left, 0, left + target_width, height))

        image = _to_rgb(image)

        if max(image.size) > MAX_DIMENSION:
            log(f"Image dimensions exceed {MAX_DIMENSION}px, resizing...")
            image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)

        return _encode_palette_png(image)


def create_client(access_key_id, secret_access_key):
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(
            s3={"addressing_style": "path"},
            # R2 does not need the checksums newer boto3 versions add by default.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def upload_image(client, png_data):
    """Upload PNG bytes as <year>/<month>/<md5>.png and return the public URL."""
    now = datetime.now()
    key = f"{now:%Y}/{now:%m}/{hashlib.md5(png_data).hexdigest()}.png"
    client.put_object(Bucket=BUCKET, Key=key, Body=png_data, ContentType="image/png")
    return f"{BASE_URL}/{key}"


def process_image(client, image, crop_wide=True):
    """Download if needed, convert and upload one image. Return its URL, or None on failure."""
    if is_url(image):
        log(f"Downloading: {image}")
        try:
            source = BytesIO(download_image(image))
        # Pillow raises many exception types on corrupt input, so catch them all.
        except Exception as error:
            log(f"Download failed: {image} ({error})")
            return None
    elif Path(image).is_file():
        source = image
    else:
        log(f"Skip: File not found - {image}")
        return None

    log(f"Processing: {image}")

    try:
        png_data = convert_image(source, crop_wide)
    except Exception as error:
        log(f"Conversion failed: {image} ({error})")
        return None

    url = None
    try:
        url = upload_image(client, png_data)
        log(f"Uploaded: {image}")
    except (BotoCoreError, ClientError, OSError, HTTPException) as error:
        log(f"Upload failed: {image} ({error})")

    log("---")
    return url


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Credentials are read from R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY. "
        "R2_BUCKET, R2_ENDPOINT_URL and R2_BASE_URL override the upload target.",
        allow_abbrev=False,
    )
    parser.add_argument("images", nargs="*", metavar="image", help="local image path or http(s) URL")
    parser.add_argument(
        "-nc", "--no-crop", action="store_true", help="do not center-crop images wider than 2:1"
    )
    args = parser.parse_intermixed_args(argv)

    if not args.images:
        parser.print_usage(sys.stderr)
        return 1

    access_key_id = os.environ.get("R2_ACCESS_KEY_ID")
    secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not access_key_id or not secret_access_key:
        log("Error: R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY must be set.")
        return 1

    client = create_client(access_key_id, secret_access_key)
    urls = [process_image(client, image, not args.no_crop) for image in args.images]

    print()
    print("Public URLs:")
    for url in urls:
        if url:
            print(url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
