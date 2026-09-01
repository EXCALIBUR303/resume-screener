# ADR-0022 — Audio answers: measured, and not scored

**Status:** accepted · **Date:** 2026-09-01 · **Closes the last open item in** M14 · **Depends on** ADR-0003, ADR-0014

## The question

M14 lists "audio answers via `faster-whisper`". The interesting question was never
whether transcription works — it does, `tiny` transcribes 6.5 seconds of speech
in 0.2s on a laptop CPU. It was whether **a transcript can carry the evidence
this system scores on.**

That matters here more than it would elsewhere. The deterministic term matches
skill names as case-insensitive substrings, and every competency is gated on a
quote that appears verbatim in the source (ADR-0003). Both read text. If the
text is a machine's guess at what it heard, both are reading a guess.

`evals/audio/asr_vocabulary.py` runs the measurement. Synthesised speech, three
conditions, two model sizes, with and without a glossary of the job's required
skills passed as `initial_prompt`.

## What it found

**Recall: a fifth to a quarter of skill mentions do not survive.**

| model | glossary | skill mentions surviving |
| --- | --- | --- |
| tiny | no | 71% |
| small | no | 79% |
| tiny | **yes** | **100%** |
| small | **yes** | **100%** |

`PostgreSQL` was lost by *every* model size without a glossary. So was `FastAPI`.
A candidate who says "I designed PostgreSQL schemas" and is transcribed as
"PostGhost Schemers" scores zero on PostgreSQL — penalised for the transcriber.

The glossary fixes it completely, and it is information the system already has:
the job posting lists its required skills. That looked like the answer.

**Precision: it is not the answer.**

The obvious hazard with a glossary is that biasing the decoder toward the skills
the job wants makes it *hear* those skills. So the same corpus includes six
answers with no technical vocabulary at all, and six more of ordinary English
that merely sounds technical.

The control set is clean — zero invented skills, with or without the glossary.
The near-miss set is not:

```
Airflow  <- The airflow in the building was poor so we opened the windows.
Python   <- The python at the zoo was fed once a fortnight by the keeper.
Spark    <- She brought a spark of creativity to every meeting we ran.
```

**Three of six clips.** Identical with the glossary and without it, and identical
across model sizes.

That last detail is the finding. This is not a transcription error — the
transcripts are *correct*. It is the matching rule: a case-insensitive substring
search cannot tell "I wrote Python" from "the python at the zoo", and neither
can `score_deterministic`.

## The decision

**Audio answers do not enter the scoring path, and `faster-whisper` is not a
dependency of this project.**

The same weakness exists for written resumes, and ADR-0014 already deals with
it: keyword stuffing is caught by requiring evidence, and evidence is what makes
a named skill count. But a written resume is *composed*. Nobody writes "the
python at the zoo" on one. A spoken answer is full of ordinary English, which
is precisely where the substring rule breaks, so the same rule that is
serviceable on prose becomes unreliable on speech.

Adding transcription without solving that would produce scores that look exactly
as trustworthy as the text ones and are not. The evidence gate would not help:
it verifies a quote against the transcript, and the transcript is the corrupted
artifact. **A gate that checks a claim against a source cannot detect that the
source is wrong.**

## What was built instead

The measurement, as a runnable artifact. `evals/audio/asr_vocabulary.py`
generates its own speech (macOS `say` or `espeak-ng`), so no audio is committed
and there is no corpus of anyone's voice — real candidate audio is exactly the
personal data this project refuses to hold.

It skips with exit code 2 and an explanation when `faster-whisper` is absent,
which is the normal case: **the package is deliberately not in
`requirements.lock`.** Anyone can install it and re-run.

## What this does not establish

Synthesised speech: clean, single-speaker, unaccented, no background noise.
These numbers are a **best case**, and real speech with accents and cross-talk
would be worse in both directions. Eleven technical clips, six control, six
near-miss — small n.

The near-miss result does not need large n to matter, because it is not a
statistical claim. It is a demonstration that three ordinary English sentences
produce a skill name under the exact rule the scorer uses. One would have been
enough.

## What would change the decision

Not a bigger model — `small` was worse than `tiny`-with-glossary on recall and
identical on precision. What is needed is a matching rule that reads meaning
rather than substrings: requiring the skill to appear in a *demonstrative* clause,
or scoring the answer against the interview rubric anchors that already exist,
rather than scanning it for keywords.

That is a real design, and it is more work than "add faster-whisper". It is
recorded here rather than half-built.

## A note on the dependency, had it been added

`faster-whisper` pulls `av` (PyAV, ~44 MB), which bundles ffmpeg — a decoder
with a long CVE history, in the container that handles attacker-controlled bytes.
It is avoidable: `transcribe()` accepts a float32 array, so decoding 16-bit mono
PCM with the standard library's `wave` module keeps untrusted audio away from
ffmpeg entirely. `evals/audio/asr_vocabulary.py` does exactly that, and the
docstring on `decode_wav` says why. Recorded because it is the right shape for
whoever builds this later.
