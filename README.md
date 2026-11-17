# custom-uploader

A reusable, resumable upload server: static Hugging Face Space frontend + Modal FastAPI/tus
backend that publishes directly to a Hugging Face dataset. Chunks are staged temporarily on a
Modal Volume and deleted immediately after the Hub commit, so no audio is retained on the server.

Live example (LEIN Speech):

- **Upload portal:** https://huggingface.co/spaces/michsethowusu/lein-speech-upload
- **Public dataset:** https://huggingface.co/datasets/michsethowusu/lein-speech-uploads
- **API:** https://michsethowusuwfp3--lein-speech-upload-web-app.us-east.modal.run

No access code is required — anyone with the portal link can upload.

## How it works

1. The shareable page is a **free static HF Space** (`sdk: static`, no compute cost). It hosts
   [Uppy](https://uppy.io/) with the tus uploader and GoldenRetriever session recovery.
2. Stations fill in only **station name, email and language**, then drop files or an entire folder
   (with subfolders). Uppy sends each file as **16 MB tus chunks** to a **Modal FastAPI/ASGI**
   endpoint. Each chunk reports its offset; dropped connections resume from the last acknowledged
   byte. GoldenRetriever restores the file selection and Uppy/tus fingerprints after a tab crash or
   reload.
3. Modal stages chunks only in a small `modal.Volume` at `/staging/uploads/<upload-id>/data.bin`.
   On the final chunk, a separate Modal function computes SHA-256 and calls
   `huggingface_hub.HfApi.upload_file` against the dataset. `hf_xet` handles chunk-level dedup and
   retries. The staged file is deleted immediately after the Hub commit, so **no audio is retained
   on Modal**. Only tiny JSON status files remain until the 24-hour cleanup job runs.
4. Every file gets a sibling JSON provenance record under `metadata/` (station, email, language,
   original relative path, size, SHA-256). The station form is also saved once per browser session
   under `submissions/<station>/<yyyy-mm>/<submission-id>.json`, so contact details are easy to find
   without scanning every file. Relative folder paths from folder uploads are preserved in both the
   audio and metadata paths.

## Files

- `modal_server.py` — Modal app: tus endpoints, HF pass-through, retry, stale-upload cleanup.
- `hf_space/index.html` — station-facing UI (metadata form, Uppy dashboard, publishing status).
- `hf_space/config.js` — API URL and dataset links (edit this per deployment).
- `hf_space/README.md` — static Space repo metadata (`sdk: static`).
- `dataset_card/README.md` — dataset card pushed to the HF dataset.

## Deploy your own

```bash
# 1. Prerequisites: Modal + Hugging Face CLI logged in
modal token new
hf auth login

# 2. Create destination repos
hf repos create <USER>/<SPACE> --repo-type space --sdk static --public --exist-ok
hf repos create <USER>/<DATASET> --repo-type dataset --public --exist-ok

# 3. Create resources referenced by modal_server.py
modal volume create <VOLUME> --version=2
modal secret create huggingface HF_TOKEN=hf_xxx            # write access to <DATASET>
modal secret create lein-speech-upload \
  LEIN_DATASET_REPO=<USER>/<DATASET> \
  LEIN_SPACE_REPO=<USER>/<SPACE> --force

# 4. Edit APP_NAME / VOLUME_NAME at the top of modal_server.py, then deploy
modal deploy modal_server.py

# 5. Set the returned URL and dataset links in hf_space/config.js, then upload the frontend
hf upload <USER>/<SPACE> hf_space/index.html index.html --repo-type space
hf upload <USER>/<SPACE> hf_space/config.js config.js --repo-type space
hf upload <USER>/<SPACE> hf_space/README.md README.md --repo-type space
hf upload <USER>/<DATASET> dataset_card/README.md README.md --repo-type dataset
```

The Modal secret name `lein-speech-upload` is hardcoded in `modal_server.py`; rename it there if
you prefer a different name for another project.

## Verification

```bash
curl <MODAL_URL>/health
# {"ok":true,"service":"...","dataset":"...","max_file_size":5368709120,"chunk_size":16777216}

hf datasets info <USER>/<DATASET>
```

The end-to-end tus flow (create → partial PATCH → HEAD resume → final PATCH → status → HF commit)
was verified against the live deployment with a 32 KB WAV whose `relativePath` contained nested
folders; the published path preserved those folders. The station form record was verified at
`submissions/<station>/<yyyy-mm>/<submission-id>.json`. Test artifacts were removed.

## Operational notes

- Max file size: 5 GB. Allowed types: WAV, FLAC, MP3, M4A, AAC, OGG, Opus, WebM, ZIP, CSV,
  JSON, TXT. Adjust `CHUNK_SIZE`, `MAX_FILE_SIZE`, and `ALLOWED_EXTENSIONS` in `modal_server.py`.
- Remote paths are deterministic:
  `uploads/<station>/<yyyy-mm>/<relative-folders>/<sha256-prefix>-<filename>`, so re-uploading the
  same file is a no-op (the Hub file is detected and skipped). Folder components are sanitized,
  `..` is dropped, depth is capped at 20, and the final path is capped at 600 characters.
- Finalization is a background Modal function with retries. A failed publish can be retried from
  the status panel (`POST /uploads/<id>/retry`) without re-uploading the audio.
- Stale partial uploads are purged after 24 hours by a scheduled Modal function.
- The endpoint is intentionally open. If abuse becomes a concern, put an auth layer or rate limit
  in front of the Modal URL; the HF write token is never exposed to the browser either way.
