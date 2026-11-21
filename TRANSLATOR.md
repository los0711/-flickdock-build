# Native translator and BreezeBlue speech

## 1.8.44 behavior and maintenance

`translation_document.py` owns proven bilingual sentence ranges and a bounded
memory-only translation cache. `translator_session.py` plans each edit, submits
only missing/changed units, and atomically commits the entire successful plan.
Existing sentences survive prefix/middle/suffix insertions and deletion. Units
in the same request already provide context; before/after fields are sent only
for neighboring sentences outside the request, each at most 180 characters.
Target/source language or glossary changes produce independent cache keys.
Failed/cancelled generations retain the last successful display and never mix
partial replies into another edit. The current limits remain 16,000 input and
24,000 output characters. No copied text is logged by these modules.

`translation_alignment.py` maps a selection to its known sentence immediately.
For a partial word/phrase it waits until dragging stops, sends only the relevant
bilingual unit to an independent request lane, and caches the result. An exact
quote must exist uniquely in the other text; otherwise sentence highlighting is
retained. No proportional character/sentence-index guesses are used. UTF-16 Qt
cursor offsets are explicitly converted to/from Python code points. Translation
is not always a one-to-one word mapping; uncertain results are not presented as
precise alignments. Global target rewrites/history restored without a sentence
map conservatively start from a whole-document pair, and subsequent edits may
require rebuilding the mapping. This prioritizes correct text over maximal reuse.

Unchanged, uniquely identifiable sentences remain alignable while an edited
source awaits translation. A surviving mouse selection is reapplied after the
matching translation arrives. The baseline comparison's hover reveals earlier
and later lines on both sides of the original scroll viewport, with a symmetric
fade away from the focused center. At full extent, the reveal is about 2.5 times
the compact comparison frame height and can extend past its editor pane.
Both comparisons can be dismissed separately while retaining the current draft.
An edited source retires the previous translation baseline and comparison.

Source and target rewrite histories have independent controls. Auto-translation
after a source rewrite does not erase source undo/compare history. Pane controls
identify their own side even when the composer selection changes. Comparing may
be closed without refresh reopening it. Clipboard invitations open the existing
translator with copied input; the compact capsule no longer expands on a middle
click. Detection completion is not discarded simply because the target changed.
Same-source/target invitations select an alternate target instead of copying
Chinese straight back as Chinese.

Audio subtitles use BreezeBlue word timestamps and literal text ranges, not a
duration-based estimate of characters. Only actual EndOfMedia marks completion.
Closing the audio card removes its own side before stopping, and generation
errors are not shown as completed reading. Voice auditions prefer the provider's
native preview clip; when unavailable, a fixed sample uses the voice's saved
language and default speed/pitch, never editor text. Preview hosts receive no
provider API key. Loading/play/stop feedback stays attached to the selected row.
`speech_export.py` owns a separate request lane and background atomic file commit;
exporting cannot cancel playback or synchronously write the whole MP3 on the GUI.

Regression entry points: `test_translator_v1844`, `soak_translator_v1844.py`,
`cloud/test/translation-units.mjs`, `cloud/test/speech-preview.mjs`. The frozen
`--verify-translator` now also runs the real bundled fastText model on a worker.

The translator is a separate PySide6 utility window. It does not change shelf
sync, clipboard delivery, LAN/P2P protocols or the surface-morph host strategy.
No example/mock translation or system speech fallback is shipped.

## Current interface and request behavior

Both language selectors align with the left edge of their text pane. The circular
swap control sits on the center seam; it does not change the native window host.
The translator follows the application's theme and has no independent theme
toggle, settings page or shortcuts. Delay is adjusted from the footer popover.
Both panes show character counts, including at the 800 px minimum window width.
The rewrite composer defaults to the translation pane. The selected pane supplies
the text and receives the result. The service sends the user's instruction and
that text as a single user message. Glossary protection applies to translation.
Source rewrites retain comparison and undo. Both copy buttons suppress the
translation invitation before writing to the clipboard.
Built-in presets request a rewrite in the language already used by the selected
text. Stored copies of unmodified older presets receive the same wording update.

Translation sends glossary entries that occur in the requested units. Empty
context fields are omitted. A single unit uses a plain-text provider response
that the Worker converts to the existing unit envelope. Replies include token
counts and cache hits when reported by DeepSeek.

Translation and rewrite currently use the official `deepseek-flash` API with
thinking explicitly disabled. Model selection is server-side, not a client setting.
Speech is independently provided by BreezeBlue.

## Modules

- `translator_session.py`: input debounce, request generations, source detection,
  editable translation, revision chain, compare/undo/regenerate, local history.
- `translator_store.py`: bounded local history/presets/glossary/preferences;
  asynchronous reads and atomic journal writes. Damaged originals are preserved.
- `translator_panel.py`, `translator_widgets.py`, `translator_history.py`,
  `translator_manager.py`: native controls, two panes, full-width rewrite composer,
  history drawer and the two small term/preset managers.
- `translation_api.py` / `cloud/src/translation.ts`: DeepSeek translation/polish;
  legacy three-field translate requests remain supported.
- `breeze_speech_api.py` / `cloud/src/speech.ts`: bounded speech proxy and decoded
  audio cache. `translator_speech.py` owns actual audio playback state.
- `file_pins.py`: independent local pin file, unrelated to cloud metadata.
- `border_reveal.py`: pointer-driven highlight on existing AA outlines, with a
  spatial index and no idle animation timer or new hit-testing surface.

## BreezeBlue configuration

Provider key is **server-side only**. Add Cloudflare Worker secret
`BREEZE_API_KEY` using Wrangler's secret prompt; never write it into a source
file, installer, environment dump or command argument. Optional server variables:
`BREEZE_TTS_MODEL` (default `breeze-tts-2-multilingual`) and `BREEZE_VOICE_ID`.
Deploy the reviewed Worker changes before testing the new client against it.
An old Worker gives a visible error; the app cannot pretend that synthesis worked.

The new `/v1/speech/voices` and `/v1/speech/synthesize` routes use the existing
FlickDock account authentication to protect the provider's paid key. Public
default voices are returned with sanitized names/IDs only. Translation retains
its existing independent transport. No private provider voices or keys are
returned to the desktop.

Official protocol: <https://docs.breezeblue.ai/api-reference/text-to-speech/convert-with-timestamps>
and <https://docs.breezeblue.ai/api-reference/voices/list-voices>.
Speech uses MP3 + word timestamps, speed 0.5–2, short segments (at most 300 Unicode
characters), one synthesis in flight and one prefetched segment. Audio is kept in
bounded RAM; no model or speech SDK is bundled. Replay cache is at most 4 MiB / 8
segments and is cleared on account change. Synthesis disables provider logging.

The service does not document a numeric pitch parameter. The pitch preference is
therefore a natural-language instruction for English/Chinese; other languages
disable that control. No invented `voice_settings.pitch` is sent.

Playback uses audio-only QMediaPlayer + QAudioOutput + QBuffer, never a native
video child. Progress is based on real playback position/duration and timestamps.
Pause/resume, stop, source/target mutual exclusion, optional automatic reading,
account changes and stale callbacks are covered by synthetic provider tests.
Audio initializes only on explicit use; auto-read defaults to off.

## Verification boundaries

Run `test_translator_v1843`, `test_breeze_speech_v1843`,
`test_workspace_v1843`, existing translation/overview/trigger regressions,
`node cloud/test/speech.mjs`, `node cloud/test/translator-workbench.mjs`, and
`node cloud/node_modules/typescript/bin/tsc --noEmit` from the cloud directory.
Fixtures spend no provider credits and never read real clipboard/account data.
These tests do not establish live provider latency, speaker output, macOS native
audio behavior or genuine Explorer/Finder OLE/Cocoa drag behavior.

Release verification also exercised the real BreezeBlue API with synthetic English
and Chinese phrases and decoded its MP3 with QAudioDecoder (no speaker output).
The deployed translator backend was reconstructed from the live Worker baseline;
only the translation route, speech route and index registration were changed.
The separate backend archive records that exact deployment scope. Existing
unpublished clipboard changes in the complete source archive were not deployed.
