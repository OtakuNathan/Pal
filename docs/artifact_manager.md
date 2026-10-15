# Conversation artifact normalization

`pal.artifact` receives conversation attachments and explicitly imported local
files. It preserves the original, produces useful normalized files, and describes
what is actually available. It does not provide a separate document-reading API
to the model. A scoped `artifact_id` identifies the managed resource; the file
paths supplied after resolution are ordinary read-only inputs to suitable tools.

## Input and storage

Channel ingress and `import_artifact(path)` use the same Artifact owner. Each
attachment has a conversation scope, an original filename/extension/MIME/size,
a processing status and managed files. Unknown formats such as spreadsheets
remain usable originals with metadata; lack of a parser is not reception failure.

- UTF-8 text reuses the managed original when no decoding change is required.
  Other supported text encodings produce one UTF-8 file alongside the original.
  New text artifacts do not create chunk files.
- Images retain the original and the image representation selected by the image
  normalization policy. Each path carries its actual filename, extension and MIME.
- PDFs retain the original, per-page text and per-page images, plus a JSON page
  index. There is no duplicate full-text file or chunk set for new PDFs. The
  index identifies original page numbers, text/image paths, processing status,
  limits and failures. Empty extracted text is not proof of a visually blank page.
- Audio is retained without automatic transcription. A transcript produced by an
  external processor or an earlier version is exposed as a file when available.
- Unsupported binary formats retain the original and file information. The model
  chooses an appropriate processor if the user's task requires one.

Existing artifacts are not rewritten or deleted merely by updating this code.
Internal owner APIs can still inspect legacy representations. New model-facing
PDF descriptions use page/index navigation rather than legacy combined text.

## L1 and model-input rendering

L1 stores `ArtifactRefPartIR` references with basic identity information, not
rendered images, base64, expanded file manifests or document/transcript bodies.
The model-input projection resolves only references present in the projected
context (or the current input in the legacy fallback path). It does not inject
all hot artifacts simply because they remain in storage.

Rendering automatically provides a bounded description: filename, extension,
MIME, size, processing status/notes, original and processed paths, and PDF page
navigation. It uses the same description builder as explicit inspection; no
model tool call is needed to obtain the initial description. Stored legacy
text-preview summaries are not copied into this description.

Only image artifacts may attach pixels automatically, subject to the selected
model's vision support and the existing image budget. PDF page images are not
attached wholesale; import a specific indexed image when its pixels are needed.
Text, PDF text, transcripts and spreadsheets are never automatically inlined.
The model uses `read_file`, `run_shell`/`rg`, or a suitable external tool based on
the task. Paths alone do not establish visual inspection.

Projection produces request-only content and does not replace the durable L1
reference. Local image data URLs are generated from owned bytes; private channel
source URLs are not forwarded to the model provider. Provider codecs translate
the rendered image part to their own wire format.

## Model tools

Four indirect tools remain:

- `import_artifact(path)`: receive/normalize a local file and emit a reference.
- `search_artifacts(query?, kind?, limit?, offset?)`: find or list available
  attachments in the current conversation. Blank query lists recent files;
  follow `next_offset` while `has_more` is true. Counts distinguish a returned
  page from the total matching inventory. Discovery does not refresh every TTL.
- `inspect_artifact_info(artifact_id)`: recover the current bounded file description
  or diagnose normalization. It returns file information, not body contents.
- `inspect_artifact_state()`: inspect owner policy and scoped resource counts.

`list_artifacts`, `read_artifact`, `grep_artifact` and `select_artifact` are retired
from model discovery and invocation. Lifecycle selection/read helpers remain
internal for leases and compatibility. In particular, withdrawing the select tool
does not remove pending-event, L1 or compaction reference renewal.

## Scope, lifecycle and activation

Artifact IDs resolve only within their original conversation scope. Foreign,
missing or retired references do not expose paths or pixels. The default hot TTL
is two hours, renewal is two hours, and the hard cap is 24 hours. Read-only worker
owners validate the existing lease without renewing it. Runtime-held references
continue through the existing lease machinery.

Retirement remains crash recoverable: owned bytes and representations are
reclaimed before a record becomes a completed tombstone. Borrowed workspace
sources are not deleted. Retired manifests explain unavailability without stale
paths. Copy managed inputs to a regular workspace before editing them.

This refactor does not modify an installed runtime or its configuration. Existing
plugin code is activated by the established plugin reload path; resident core/L1
protocol changes, when present, require a separate host activation plan. The
current implementation reuses the existing typed-reference projection path.
