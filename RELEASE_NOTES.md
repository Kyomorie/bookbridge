# Release Notes - 7.3.4

This is a single-fix release: **local transcription works again on the standard
image.** Since 7.3.0, the automatic GPU check that runs before local Whisper
transcription assumed the NVIDIA CUDA libraries were at least present to inspect —
but the standard (non-`-cuda`) image ships without them entirely, so on CPU-only
installs every transcription crashed immediately with `No module named 'nvidia'` and
books never finished syncing. The check now treats missing CUDA libraries as what
they mean: use the CPU.

You are affected if you run the standard image and transcribe locally with **Whisper
Device** on its default `auto` setting. The `-cuda` image, external transcription
servers (whisper.cpp, speaches, parakeet), and Deepgram were never affected, and
neither were installs that had already set Whisper Device to `cpu` by hand — that
was the workaround, and it is no longer needed.

## Fixed

- **Local transcription no longer crashes with `No module named 'nvidia'` on the
  standard image.** Every transcription attempt on an affected install failed before
  it began, marking the book "failed, retry later" each time. Because those books
  were parked for retry, they pick themselves back up on the next sync cycle after
  you update — no manual steps needed. Reported by
  [@ibrodebill](https://github.com/ibrodebill). (#355)

## Operational Notes

No database migration is required, the BridgeSync KOReader plugin is unchanged
(0.5.4) so no plugin re-download is needed, and `requirements.txt` and the
Dockerfile are untouched. Pull the new image and restart BookBridge; parked books
retry on their own.

If you set **Whisper Device** to `cpu` as a workaround, you can leave it — it is
still a valid setting — or return it to `auto`, which now falls back to the CPU
correctly.
