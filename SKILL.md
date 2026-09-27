---
name: course-kb
description: |
  Maintain a local course knowledge base: ingest course files, maintain weekly
  indexes and concept evidence, validate integrity, and export review packs.
  Use for course-kb, 课程知识库, 周索引, 概念卡, 逐字稿入库, 课程资料归档, 复习包.
  General tutoring and exam explanation are separate workflows that consume the KB.
---

# Course Knowledge Base

You organize course evidence, attribute claims, and distinguish source content from
interpretation. Python 3.10+, standard library only. Resolve `<skill-dir>` from this
file, never the caller's working directory. All backend modules ship with this skill;
a separate development checkout or another skill is not required.

The KB defaults to `~/course-kb`, but the user may select any local directory. Resolve
`<KB>` from the current request or established project context. Keep one semester per
KB. On Windows select an available Python 3.10+ command (`py -3` or `python`); on
macOS/Linux normally use `python3`. Quote every path. Below, `<python>` means that
command. Use local storage; cloud-synced folders and network shares are not validated
for concurrent transactions. Windows support uses native file locking but has not yet
been verified on a Windows machine in this release.

## First use and existing libraries

1. Inspect the resolved root. If it has `manifest.json`, read its version and calendar.
   A valid 1.1 library is ready for use even with incomplete calendar fields; report
   gaps without repeating setup or demanding the same PDF again. For 1.0, use the
   explicit migration workflow below before writing. Unknown versions stop with an
   explanation; never replace their files.
2. If the root exists without a manifest, inspect it. Do not initialize over an existing
   directory, even if apparently empty; identify an interrupted setup or select a new
   path within the user's request. Never delete existing files to force setup.
3. For a new root, use the supplied academic-calendar PDF. If none was supplied, ask
   for the school's teaching calendar PDF and mention the default KB location. A user
   who cannot provide one may explicitly proceed with an incomplete calendar. Do not
   ask them to manually enter every date.
4. Read the calendar yourself: semester, teaching-week labels and date intervals,
   holidays, exam period and exceptional schedules. Do not guess dates or mechanically
   skip holiday weeks. Preserve the school's numbering, including `w00` and gaps;
   teaching-week count is not the largest week label. Non-numbered orientation,
   military-training or practice periods remain calendar notes. Each separate term
   uses its own KB. Ambiguous fields stay null with `_pending_input`.
5. Prepare an external manifest JSON following `references/schemas.md` and
   `examples/manifest.json`. Use `kb_version: "1.1"`, `courses: []`, the actual current
   timestamp, and actual extracted metadata. Do not copy example facts as user facts.
   An unknown `week_ranges` is `{}`; a known label with unknown dates is `{"w00": null}`.
6. Create the new library with the backend:

   ```text
   <python> "<skill-dir>/scripts/kb.py" init --root "<KB>" --manifest "<manifest-json>" --calendar-file "<calendar.pdf>"
   ```

   Omit `--calendar-file` only when no calendar file is available. The file is copied
   unchanged to `_calendar.pdf`. Initialization validates first, refuses existing roots,
   and sets `setup_complete: true` to record that setup ran, not that every field is known.
   Report unresolved fields and immediately resume the user's original import request.

## Storage and authority

- `manifest.json`: schema version, course list, semester and teaching calendar.
- `<COURSE>/course.json`: metadata, planned/actual schedule, assessment, contacts.
- `<COURSE>/raw/`: exact imported files, append-only; corrections are new versions.
- `<COURSE>/sources.json`: stable material IDs, checksums, kind, generation, verification.
- `<COURSE>/index/weekly/wNN.json`: use of materials by week, summaries and segments.
- `<COURSE>/index/concepts/<slug>.json`: definitions, appearances, separate quotations.
- `notes/`: the user's own files; do not read or alter them without explicit instruction.
- `_directory.md`: generated navigation. Review packs go outside the KB.
- `.course-kb/transactions/`: write journals and recovery backups; never hand-edit.

Read `references/schemas.md` before changing records. Keep existing raw names and
course folders. New weekly materials normally use `wNN-reading-authorYEAR.pdf`,
`wNN-slides-topic.pptx`, `wNN-transcript.md` or `wNN-summary.md`. Syllabi, assignments
and datasets may have no week prefix. Register every imported material in sources.json,
including course-level documents. Cite assignment rules in assessment/schedule fields;
a weekly source entry may also reference an assignment when relevant to that week.

Material identity and provenance belong to the registry. Concept appearances own
concept/source/week relations. Weekly and per-source concept lists, week counts and
the directory are derived. Do not rewrite definitions during structural migration;
AI reconstruction is not guaranteed to reproduce the same interpretation.

## Integrity and provenance

- Never overwrite, remove or rename an existing raw file. Never delete user material.
- Do not invent definitions, quotations, source locations or teaching coverage.
- Keep concepts per course; use `cross_course` pointers rather than merging cards.
- `kind`, `generation_method` and `verification_status` are separate. An imported AI
  summary remains byte-for-byte unchanged with `kind: summary` and
  `generation_method: ai_generated`. A generated visual may be `slides` with
  `generation_method: ai_generated`. Neither is a classroom transcript. Preserve
  links, retrieval details and restrictions in registry provenance fields or notes;
  never insert a provenance header into a raw file. Keep unsupported numbers, dates
  and attributed quotations explicitly pending verification in public index fields.
- Checksums verify bytes, not truth. `verified` means checked against the cited material,
  not proof that the material's claims are true. Do not upgrade it automatically.
- `definition` is explanatory prose. Literal excerpts belong separately in `quotes`,
  with source ID, location and verification. Legacy verbatim definitions remain
  unverified until checked. Retain derivation from AI summaries in exported evidence.
- `coverage` is optional and cannot block material registration. Missing classroom
  evidence means unknown, not that something was absent from teaching.
- `indexing_status` describes processing of available files. `material_availability`
  describes known missing materials. Do not use legacy `status` alone for completeness.
- Unknown metadata uses null plus `_pending_input`. Learning and citation caveats also
  belong in public fields such as `version_note`, `open_questions` or availability;
  underscore-prefixed fields do not appear in review packs.

## Inspect and migrate

```text
<python> "<skill-dir>/scripts/validate.py" --root "<KB>" --json
<python> "<skill-dir>/scripts/kb.py" migrate --root "<KB>" --stage "<new-empty-stage>"
<python> "<skill-dir>/scripts/kb.py" apply --root "<KB>" --stage "<new-empty-stage>"
```

For 1.0, validate, then prepare the migration, inspect changes and apply within the
user's authorized upgrade scope. The stage must be outside the KB and must not contain
it. Retain original bytes, definitions, unknown fields and user notes. Migration adds
source IDs, registries and evidence metadata. Old source-index-only links become
unverified appearances with null context/location. Week-only links without attributable
sources remain `unresolved_concepts`. No invented quotes or automatic semantic review.
Finish by validating the live library and checking an export. A migration error leaves
the live KB unchanged. Preserve the stage to investigate; do not rebuild the user's KB.

## Ingest, add a course, or update

1. Determine the course, week, kind and provenance from supplied materials. Ask only
   when consequential ambiguity cannot be resolved. For a new course, obtain a usable
   course code and name; instructor/total weeks/language may remain null pending evidence.
2. Capture the baseline before editing:

   ```text
   <python> "<skill-dir>/scripts/kb.py" prepare --root "<KB>" --stage "<new-stage>"
   ```

3. In the stage, copy existing metadata files that need editing and preserve their
   records. Add new raw files to `<stage>/<COURSE>/raw/`; names must not collide with
   existing originals. For a new course, edit the staged manifest and add course.json,
   raw/, index/weekly/ and index/concepts/. See examples for metadata shapes. The backend
   does not need to create or populate notes/.
4. Read complete relevant material. Cross-read slides and transcripts for emphasis,
   instructor additions and criticism. Write summaries, segments, definitions and
   appearances in the stage. An appearance uses `assertion_basis: extracted`, its
   actual filename/week, and null for unknown locations. Known source IDs stay stable.
   Newly imported files receive IDs during prepare. Quotes require actual source reading.
5. Review provenance in staged sources.json; prepare conservatively registers new files.
   An unindexed material can still be registered. Never edit a hash to hide changed raw.
   For known sources, appearances determine the derived key_concepts lists.
6. Run prepare again. It registers materials, fills IDs, rebuilds derived lists/counts,
   generates the directory and validates the full candidate without writing the live KB.
   Inspect the proposed files and warnings. Resolve facts from evidence, not guesswork.
7. Apply and verify:

   ```text
   <python> "<skill-dir>/scripts/kb.py" apply --root "<KB>" --stage "<stage>"
   <python> "<skill-dir>/scripts/validate.py" --root "<KB>" --json
   ```

   Apply locks writers, retains backups and journals publication. Identical reapplication
   is a no-op. A stale stage is rejected; preserve it and reconcile in a fresh stage.
   Direct writes to live metadata bypass these protections and are not this workflow.
8. Report what was imported, important ambiguities and verification gaps. Never report
   success when validation has errors. Warnings disclose unresolved work without forcing
   invented data. Check the requested export as well as the structural result.

## Content reading

Read the relevant transcript end to end; split long material into ordered chunks.
Search locates passages but does not replace reading. Extract every statement that
sets an obligation, prohibition, date, number or changed arrangement, including exams,
assessment, assignments, integrity rules, software requirements, schedule and contacts.
Rules can occur inside examples or software demonstrations, outside obvious headings.

Semester-long policies go into assessment/schedule/contact fields with source references.
Temporary announcements remain in the weekly index. Preserve differing accounts of a
changed deadline and flag uncertainty. Do not claim a source says nothing unless you
actually read it; distinguish that from failing to locate a passage in a search.

Pre-class readings are normal: index them immediately with `status: pre-class`, then
add classroom evidence when available. A working topic inferred from readings is allowed
when identified as provisional in public notes; do not invent an official syllabus title.
Concept counts follow the material, not a quota. Recurring examples can have cards tagged
`course-thread` when they are useful standalone study topics.

## Recovery and exports

If an interrupted update leaves a lock/journal, readers refuse partial state. After the
writer has ended, run `scripts/kb.py recover --root "<KB>"`, then validate. Recovery
restores unfinished metadata, keeps newly imported raw files, and refuses to overwrite
later user edits. Completed batches retain backups; recover does not undo a committed
batch. Prepare a separate restoration batch when a deliberate rollback is needed.

```text
<python> "<skill-dir>/gen_pack.py" COURSE --tier 2 --weeks w01-w03 --root "<KB>" --out "<output-dir>"
<python> "<skill-dir>/gen_tier4.py" COURSE w01 --root "<KB>" --out "<output-dir>"
<python> "<skill-dir>/gen_directory.py" --root "<KB>"
<python> "<skill-dir>/gen_snapshot.py" --root "<KB>" --out "<snapshot.md>"
```

Tier 1: compact summaries/concepts. Tier 2: full cards with all appearances, including
other weeks for context. Tier 3: complete public weekly metadata. Every tier retains
provenance restrictions and verification state. Review packs are rendered views; edit
staged source indexes, not exported files. Split packs over 50K characters by week range.

Tier 4 is imported transcript text with actual provenance. It rejects AI summaries and
AI-generated sources. For multiple transcripts specify `--source` (filename or source ID)
or unambiguous `--suffix`. `--start MM:SS --end MM:SS` selects enclosing timestamped
segments with actual boundaries disclosed and distinct output filenames. Full exports
preserve the original body bytes. Export selection must never silently omit other sources.

The directory stays within 4000 characters by reducing concept listings while retaining
course/week titles. If titles alone exceed the limit, it fails with an explanation.
Snapshot exports disclose their selected detail and do not read user note contents.
External sync, messaging and publication are outside this local backend's scope and need
the corresponding user authorization. Do not proactively reorganize an idle KB.
