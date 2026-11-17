---
title: LEIN Speech — Station Audio Upload
emoji: 🎙️
colorFrom: blue
colorTo: green
sdk: static
app_file: index.html
pinned: false
---

# LEIN Speech — Station Audio Upload

An open, shareable upload portal for LEIN partner radio stations. Stations fill in recording
details and drop audio files into the uploader; no access code is required. Uploads are resumable
(Uppy + tus, 16 MB chunks) and are published directly to the public
[LEIN Speech uploads dataset](https://huggingface.co/datasets/michsethowusu/lein-speech-uploads).

The frontend is static and free to host; the upload API runs on Modal and only stages chunks
temporarily before passing them to Hugging Face. No HF token is exposed to the browser.
