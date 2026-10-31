# picbedmanager

Converts images to palette PNGs, uploads them to Cloudflare R2 and prints their public URLs.

## Install

```sh
pip install -r requirements.txt
```

## Configure

Set all of these environment variables:

| Variable | Value |
|---|---|
| `R2_ACCESS_KEY_ID` | R2 access key ID |
| `R2_SECRET_ACCESS_KEY` | R2 secret access key |
| `R2_BUCKET` | Bucket name |
| `R2_ENDPOINT_URL` | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BASE_URL` | Public URL of the bucket, without a trailing slash |

## Run

```sh
python upload_to_cloudflare.py [-nc] image [image ...]
```

`image` is a local path or an `http(s)` URL. A URL starting with `<R2_BASE_URL>/` is printed unchanged, without being downloaded or uploaded. `-nc`, `--no-crop` keeps images wider than 2:1 uncropped.

Each image is uploaded as `<R2_BASE_URL>/<year>/<month>/<md5>.png` after these changes:

1. Applies EXIF rotation, then removes metadata.
2. Center-crops images wider than 2:1 to 2:1.
3. Scales images down to fit within 2048×2048 px.
4. Reduces colors to a palette of up to 256.

URLs are printed to stdout and progress to stderr. Images that fail are reported on stderr and left out of the URLs. Downloads are limited to 50 MB and a 30-second timeout.

The exit code is 1 if no image is given or credentials are missing, and 0 otherwise, even if every image fails.
