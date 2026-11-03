# picbedmanager

Uploads files to Cloudflare R2 and prints their public URLs. Images are converted to palette PNGs first.

## Install

```sh
pip install -r requirements.txt
```

## Configure

Set all of these environment variables:

| Variable | Value |
|---|---|
| `R2_ENDPOINT_URL` | R2 S3 API endpoint URL |
| `R2_ACCESS_KEY_ID` | R2 access key ID |
| `R2_SECRET_ACCESS_KEY` | R2 secret access key |
| `R2_BUCKET` | Bucket name |
| `R2_BASE_URL` | Public URL of the bucket, without a trailing slash |

## Run

```sh
python upload_to_cloudflare.py [-nc] file [file ...]
```

`file` is a local path or an `http(s)` URL. A URL starting with `<R2_BASE_URL>/` is printed unchanged, without being downloaded or uploaded. `-nc`, `--no-crop` keeps images wider than 2:1 uncropped.

Files are uploaded to `<R2_BASE_URL>/<year>/<month>/<md5>.<ext>`:

| File | `<ext>` | Content type |
|---|---|---|
| SVG | `svg` | `image/svg+xml` |
| Any other image Pillow can read, including HEIC and AVIF | `png` | `image/png` |
| Other text | original | `text/plain` |
| Other binary | original | guessed from the extension, or `application/octet-stream` |

The original extension is omitted if the file has none. A file whose extension names an image type but that Pillow cannot read fails instead of being uploaded.

Images converted to PNG get these changes:

1. Applies EXIF rotation, then removes metadata.
2. Center-crops images wider than 2:1 to 2:1.
3. Scales images down to fit within 2048×2048 px.
4. Reduces colors to a palette of up to 256.

URLs are printed to stdout and progress to stderr. Files that fail are reported on stderr and left out of the URLs. Downloads are limited to 50 MB and a 30-second timeout.

The exit code is 1 if any of these variables is unset or empty, or no file is given, and 0 otherwise, even if every file fails.
