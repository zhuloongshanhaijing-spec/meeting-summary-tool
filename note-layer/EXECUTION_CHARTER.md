# Meeting note-evidence prototype: execution charter

## Outcome

Add a local, evidence-preserving note layer to the Meeting Records v3 test
pipeline.  It must link supplied Markdown notes to already-created audio
literal records without changing the literal record or treating a note as a
speaker quotation.

## Inputs and privacy

- Audio and notes remain in `<your raw lecture materials directory>` and are read-only.
- Existing bounded local ASR outputs are reused from `../lectures-benchmark-20260911/`.
- All prototype code, intermediate files, and rendered test output live under
  this directory.  No cloud upload, account, credential, or paid service is
  permitted.

## Proposed data flow

`manifest note -> Nxxxxxx note evidence (path + line range)`
`audio literal record -> deterministic candidate retrieval`
`bounded local relation classifier -> supports | partial_support | conflict |
possible_related | unknown`
`renderer -> transcript unchanged + separate note corroboration section`

## Non-goals

- No attempt to repair ASR text from a note.
- No claim that a note was spoken.
- No archive/move/delete of source files.
- No full 104-minute run in this prototype.

## Acceptance gates

1. Every note chunk has a source path, line locator, stable ID, and explicit
   disposition.
2. The generated literal transcript is byte-identical before and after note
   linking.
3. A note link never changes transcript certainty or raw/clean text.
4. Relations may only be `supports`, `partial_support`, `conflict`,
   `possible_related`, or `unknown`; only listed record IDs may be referenced.
5. The final test package displays note text only in a clearly labelled note
   section and includes a machine-readable receipt.
6. Syntax and focused tests pass; a real bounded-input package is rendered.

## Stop rules

Stop at `BLOCKED` if the local model is unavailable, the classifier returns
invalid JSON twice, a required ID cannot be resolved, or any check suggests a
note has altered literal transcript content.
