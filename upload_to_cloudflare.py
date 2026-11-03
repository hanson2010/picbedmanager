#!/usr/bin/env python3
"""Upload files to Cloudflare R2 and print their public URLs. Images are converted to palette PNGs
first; SVGs and other files are uploaded unchanged."""

import argparse
import hashlib
import mimetypes
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

register_heif_opener()

ENV_VARS = (
    "R2_ENDPOINT_URL",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_BASE_URL",
)

MAX_DOWNLOAD_BYTES = 50_000_000
DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_DIMENSION = 2048
PALETTE_COLORS = 256

# XML declaration, processing instructions, comments and doctype, then the <svg> root element.
SVG_PATTERN = re.compile(
    rb"\s*(<\?.*?\?>\s*|<!--.*?-->\s*|<!DOCTYPE[^>\[]*(\[.*?\])?\s*>\s*)*<svg[\s>]", re.DOTALL
)

# Text types that are not text/*. All text is served as text/plain so that HTML, XML and
# scripts are never rendered or run from the bucket's domain.
TEXT_MIME_TYPES = {
    "application/ecmascript",
    "application/javascript",
    "application/json",
    "application/x-sh",
    "application/xml",
}


@dataclass(frozen=True)
class Settings:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    base_url: str


def log(message):
    print(message, file=sys.stderr)


def load_settings():
    """Return Settings read from ENV_VARS, or None after logging any that are unset."""
    values = {name: os.environ.get(name, "") for name in ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        log(f"Error: {', '.join(missing)} must be set.")
        return None
    return Settings(
        endpoint_url=values["R2_ENDPOINT_URL"],
        access_key_id=values["R2_ACCESS_KEY_ID"],
        secret_access_key=values["R2_SECRET_ACCESS_KEY"],
        bucket=values["R2_BUCKET"],
        base_url=values["R2_BASE_URL"],
    )


def is_url(value):
    return re.match(r"https?://", value) is not None


def is_svg(data):
    return SVG_PATTERN.match(data.removeprefix(b"\xef\xbb\xbf")) is not None


def is_utf8_text(data):
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def file_extension(source):
    """Return the lowercase extension of a path or URL without the dot, or "" if it has none."""
    path = PurePosixPath(unquote(urlparse(source).path)) if is_url(source) else Path(source)
    extension = path.suffix.lower().removeprefix(".")
    return extension if extension.isascii() and extension.isalnum() else ""


def guess_mime_type(extension):
    return mimetypes.guess_type(f"file.{extension}")[0] if extension else None


def passthrough_content_type(data, extension):
    """Return the content type to serve a file that is uploaded unchanged with."""
    mime_type = guess_mime_type(extension)
    if is_utf8_text(data):
        return "text/plain; charset=utf-8"
    if mime_type and (
        mime_type.startswith("text/") or mime_type.endswith("+xml") or mime_type in TEXT_MIME_TYPES
    ):
        return "text/plain"
    return mime_type or "application/octet-stream"


def download_file(url):
    """Return the bytes of the file at url, or raise if it is too large."""
    request = Request(url, headers={"User-Agent": "picbedmanager"})
    with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"file is larger than {MAX_DOWNLOAD_BYTES} bytes")
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


def create_client(settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name="auto",
        config=Config(
            s3={"addressing_style": "path"},
            # R2 does not need the checksums newer boto3 versions add by default.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def upload_file(client, settings, data, extension, content_type):
    """Upload bytes as <year>/<month>/<md5>[.<extension>] and return the public URL."""
    now = datetime.now()
    suffix = f".{extension}" if extension else ""
    key = f"{now:%Y}/{now:%m}/{hashlib.md5(data).hexdigest()}{suffix}"
    client.put_object(Bucket=settings.bucket, Key=key, Body=data, ContentType=content_type)
    return f"{settings.base_url}/{key}"


def process_file(client, settings, file, crop_wide=True):
    """Download if needed, convert and upload one file. Return its URL, or None on failure.

    SVGs are uploaded unchanged as image/svg+xml. Images Pillow can read are converted to
    palette PNGs. Anything else is uploaded unchanged, unless its extension says it is an
    image, in which case it is reported as a failed conversion.
    """
    if file.startswith(f"{settings.base_url}/"):
        log(f"Already hosted: {file}")
        log("---")
        return file

    if is_url(file):
        log(f"Downloading: {file}")
        try:
            data = download_file(file)
        except (OSError, HTTPException, ValueError) as error:
            log(f"Download failed: {file} ({error})")
            return None
    elif Path(file).is_file():
        try:
            data = Path(file).read_bytes()
        except OSError as error:
            log(f"Read failed: {file} ({error})")
            return None
    else:
        log(f"Skip: File not found - {file}")
        return None

    log(f"Processing: {file}")

    extension = file_extension(file)
    if is_svg(data):
        extension, content_type = "svg", "image/svg+xml"
    else:
        try:
            data = convert_image(BytesIO(data), crop_wide)
            extension, content_type = "png", "image/png"
        except UnidentifiedImageError as error:
            if (guess_mime_type(extension) or "").startswith("image/"):
                log(f"Conversion failed: {file} ({error})")
                return None
            content_type = passthrough_content_type(data, extension)
        # Pillow raises many exception types on corrupt input, so catch them all.
        except Exception as error:
            log(f"Conversion failed: {file} ({error})")
            return None

    url = None
    try:
        url = upload_file(client, settings, data, extension, content_type)
        log(f"Uploaded: {file}")
    except (BotoCoreError, ClientError, OSError, HTTPException) as error:
        log(f"Upload failed: {file} ({error})")

    log("---")
    return url


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"Requires the environment variables {', '.join(ENV_VARS)}.",
        allow_abbrev=False,
    )
    parser.add_argument("files", nargs="*", metavar="file", help="local file path or http(s) URL")
    parser.add_argument(
        "-nc", "--no-crop", action="store_true", help="do not center-crop images wider than 2:1"
    )
    args = parser.parse_intermixed_args(argv)

    settings = load_settings()
    if settings is None:
        return 1

    if not args.files:
        parser.print_usage(sys.stderr)
        return 1

    client = create_client(settings)
    urls = [process_file(client, settings, file, not args.no_crop) for file in args.files]

    print()
    print("Public URLs:")
    for url in urls:
        if url:
            print(url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
