# Course KB schema 1.1

The canonical contract is maintained with `course_kb/validation.py` and its regression
fixtures. JSON must have no duplicate keys, NaN or Infinity. Unknown schema versions
fail closed. Version 1.0 remains readable; explicit migration produces version 1.1.

## Root and course metadata

`manifest.json` requires `kb_version`, `owner` (string or null), `semester`,
`institution`, `program`, `courses`, `last_updated`, and `calendar`.
`courses` contains unique `{code, name, directory}` objects; directory is one relative
child directory. `calendar` has `source`, `term`, `teaching_weeks`, and `week_ranges`:
`{"w01": "2025-09-01 / 2025-09-07"}`. Preserve calendar exceptions and original source
references. Timestamps are ISO 8601 with timezone. Only the explicit `init` command
creates a new KB; ordinary reads never silently create a missing root.

Required fields must exist, but explicit unknown values are allowed: manifest
`semester`, `institution`, `program`; calendar `source`, `term`, `teaching_weeks`;
course `instructor`, `total_weeks`; and weekly `date_range` may be null. They produce
pending warnings, not invented defaults. Missing required fields and wrong types are
still errors. Keep `_pending_input` descriptions with unresolved metadata.

`week_ranges: {}` means the calendar has not been obtained; it warns and allows work.
`{"w00": null}` records a known week label whose dates remain unknown. Nonempty
calendars define the permitted week labels; add an explicit null entry for a known
week before using it when its dates are missing. Labels use w00 through w99 and may
start above 1 or contain gaps. `teaching_weeks`/`total_weeks` are counts, not maximum
week numbers. Known dates must be valid and agree with their indexed week.

Optional `setup_complete: true` means first-run setup was performed, not that all
calendar facts are known. Do not repeatedly trigger setup solely because dates remain
unknown. `init --manifest <JSON> [--calendar-file <PDF>] --root <new-path>` validates
a schema 1.1 manifest with an empty course list, copies a supplied calendar unchanged,
and refuses existing roots. Add courses later through staged updates.

`<COURSE>/course.json` requires `code`, `name`, `instructor`, `total_weeks`,
`weeks_indexed`, `language` (`en`, `zh`, `mixed`, or pending null), and `created`.
The code matches the manifest; `weeks_indexed` is derived. Optional `schedule`,
`assessment`, `contacts`, and `notes` retain existing structure and content. Schedule
and assessment use `components` arrays, which may be empty. Assessment weights can be
null. Calendar dates and deadlines should be absolute, never 'next Monday'.

Schedules may contain `planned_topic` and `actual_topic`; preserve syllabus plans.
Semester-long rules belong in course.json; temporary announcements belong in weekly
indexes. Record disagreements between syllabus and spoken announcements explicitly;
use the syllabus's grading policy unless a confirmed official revision supersedes it.

## Material registry: sources.json

One course owns one registry, including course-level syllabi, assignments and datasets.
Filenames retain their existing values. IDs remain stable once assigned, independent
of the KB's absolute path. Do not reuse an ID for changed file contents.

```json
{
  "schema_version": "1.1",
  "sources": [{
    "source_id": "src-00000000-0000-0000-0000-000000000001",
    "filename": "w01-reading-example.pdf",
    "kind": "reading",
    "generation_method": "unknown",
    "verification_status": "unverified",
    "sha256": "<64 lowercase hex digits; example placeholder>",
    "original_filename": null,
    "version_note": null,
    "derived_from": []
  }]
}
```

All listed fields are required. `kind`: reading, slides, transcript, summary, syllabus,
assignment, dataset, other. `generation_method`: original, asr, ai_generated, unknown.
`verification_status`: unverified, verified. Unknown creation method stays unknown.
A file checksum validates bytes, not the truth or accuracy of its contents.

`derived_from` contains source IDs within this course. No self-reference or invented
parent. If the recording is unavailable, preserve its external origin in provenance
notes; do not pretend a local recording exists. New material may be registered before
being indexed; unregistered raw files produce a warning so interrupted imports can be
recovered without deleting original files. Missing referenced files and changed stored
checksums are errors.

## Weekly indexes

Keep `course`, `week`, `status`, `date_range`, `topic`, `sources`, `key_concepts`,
`weekly_summary`, and `open_questions`. Week number matches `wNN.json` and the calendar.
`status` is legacy compatibility: complete, partial, pre-class.

New required fields:

- `indexing_status`: complete or partial, processing of available material only.
- `material_availability`: `{ "status": "unknown", "missing": [] }`; status can be
  unknown, incomplete, complete. Missing is an array of evidence-grounded descriptions.
- `unresolved_concepts`: array of existing card slugs known only at the weekly level,
  for which the source association has not yet been established.

Each `sources` item retains its existing title/author/year/edition/version_note,
recording date/duration, outline/core_argument, and segments. It requires `filename`,
`source_id`, `type` (equal to registry kind), and derived `key_concepts`. Registry metadata
owns provenance classification; week entries own use and explanation in that week.
Do not repeat a source within a weekly index; the same source may be used in other weeks.

`weekly.key_concepts` is the union of appearances in that week and unresolved concepts.
`source.key_concepts` is the set of appearances for that week and source. `prepare`
rebuilds both from concept appearances, so edit appearances rather than those lists.
During the initial 1.0 migration only, old source-index associations are preserved as
unverified appearances with null context/location; they are not treated as newly read
source evidence.

Segments retain `topic`, `approx_start`, `key_points` and optional `slide_refs`.
Qualified slide references use `filename.pdf#p2-5`. Bare `p2-5` references are ambiguous
if there are several slide files and generate a warning. New precise positions use
locators below. Invalid old references such as `p0-1` stay visible as warnings until
someone checks the source; migration must not guess a replacement page.

Summaries attribute claims to the author or instructor. Distinguish reading arguments,
classroom additions, criticism and AI summary compression. Record known gaps in public
fields as well as internal pending lists when they affect learning or citation.

## Concept cards and evidence

Cards require `name`, `slug`, `aliases`, `definition`, `originator`, `appearances`,
`related`, `tags`, and `quotes`. Slug matches the lowercase ASCII hyphenated filename.
Definitions and originators may be null; aliases/related/tags/quotes are arrays.

If definition is non-null, `definition_basis` is synthesized, verbatim, or unverified.
If null, omit definition_basis. Synthesized prose is a paraphrase and must not be
presented as a quotation; it can support study when its sources and limits are retained.
A verbatim definition requires an independently recorded verified quotation with a
source and valid location. The validator checks structure and attribution, not whether
the text really matches the source. Human/Agent source inspection is still necessary.

Initial migration preserves every definition string. A former verbatim marker is moved
to `legacy_definition_basis`, with `definition_basis: unverified` and no invented quote.

An appearance is:

```json
{
  "week": 1,
  "source_file": "w01-reading-example.pdf",
  "source_id": "src-00000000-0000-0000-0000-000000000001",
  "context": null,
  "locator": null,
  "verification_status": "unverified",
  "assertion_basis": "legacy_source_index"
}
```

`assertion_basis`: legacy_appearance, legacy_source_index, extracted. The source must
exist in that week's source list and in the registry. Context describes how the concept
was used; null explicitly means the old index asserted the link without explaining it.

`locator` is null or a nonempty object containing known fields:

- `pdf_page`: positive physical PDF page number, starting at 1.
- `printed_page`: string, for printed labels such as `iv` or `86`.
- `slide`: positive slide number, starting at 1.
- `start_seconds`, `end_seconds`: nonnegative integers; end must not precede start.
- `segment_id`: string identifying a recorded segment.

Do not parse free-text page mentions into verified locators mechanically. `coverage`
is optional: both, slides-only, class-only, reading-only, unknown. Recording a new source
never makes coverage mandatory for unrelated concepts. Absence of a transcript does
not establish that the instructor did not teach a concept.

`quotes` contains `{text, source_id, locator, verification_status}`. Quote text stays
separate from explanatory definition/context. A verified quote needs a known source,
nonempty text and location; a valid record is not proof of semantic or textual accuracy.

`related` points to cards in this course. `cross_course` may contain `{course, slug,
note}` and must resolve through manifest course codes, including when a directory name
differs. Never merge separate courses' definitions into a global card.

## Workflow state and compatibility

Underscore-prefixed fields are internal workflow metadata, omitted from review packs.
Keep unknown fields when updating existing records; do not drop annotations. Optional
`last_updated` timestamps preserve their timezones. Notes under the user's `notes/`
directories are not part of this schema or migration.

Errors block publication. Warnings preserve review work that cannot be solved without
checking material: legacy page anomalies, unregistered imported files, and other
explicit gaps. A successful validation does not claim that the course's factual content
or every quotation has been independently verified.
