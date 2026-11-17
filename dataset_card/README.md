---
pretty_name: LEIN Speech — Radio Station Uploads
license: cc-by-4.0
language:
  - lir
  - en
task_categories:
  - automatic-speech-recognition
  - text-to-speech
tags:
  - audio
  - speech
  - liberian-english
  - kolokwa
  - radio
  - lein
size_categories:
  - n<1K
---

# LEIN Speech — Radio Station Uploads

Raw audio recordings submitted by partner radio stations for the LEIN Speech Dataset project.
This is the landing bucket for the public upload portal at
[spaces/michsethowusu/lein-speech-upload](https://huggingface.co/spaces/michsethowusu/lein-speech-upload).

Files are published as soon as a station's resumable upload completes. Stations provide only their
name, email and language; whole folders (including subfolders) can be uploaded and their structure
is preserved. Each audio file has a matching JSON record under `metadata/` with the station, email,
language, original relative path, file size and SHA-256 checksum.

The station form itself is saved separately as `submissions/<station>/<yyyy-mm>/<submission-id>.json`
whenever an upload starts, so the contact details can be retrieved without scanning every audio
metadata file. One submission record is reused for all files selected in a browser session.

## Repository layout

- `uploads/<station>/<yyyy-mm>/<relative-folders>/<sha256-prefix>-<filename>` — submitted audio, ZIP batches and sidecars
- `metadata/<station>/<yyyy-mm>/<relative-folders>/<sha256-prefix>-<name>.json` — provenance record for each file
- `submissions/<station>/<yyyy-mm>/<submission-id>.json` — station name, email and language per upload session

## License

Submitted recordings are shared under CC BY 4.0. By uploading, stations confirm they have
permission to share the recordings publicly.

## Notes

This dataset is a raw ingestion point. Cleaned, enhanced and packaged derivatives live in
[michsethowusu/liberian-english-speech](https://huggingface.co/datasets/michsethowusu/liberian-english-speech)
and [michsethowusu/liberian-english-speech-packages](https://huggingface.co/datasets/michsethowusu/liberian-english-speech-packages).
