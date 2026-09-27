"""只读验证知识库的结构、来源和派生关系，不判断内容是否真实。"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .model import DataError, assert_readable, load_json, safe_path, sha256_file

KINDS = {"reading", "slides", "transcript", "summary", "syllabus", "assignment", "dataset", "other"}
GENERATIONS = {"original", "asr", "ai_generated", "unknown"}
VERIFICATIONS = {"unverified", "verified"}
COVERAGES = {"both", "slides-only", "class-only", "reading-only", "unknown"}
ASSERTIONS = {"legacy_appearance", "legacy_source_index", "extracted"}
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
WEEK = re.compile(r"^w([0-9]{2})$")
PAGE_REF = re.compile(r"^p([1-9][0-9]*)(?:-([1-9][0-9]*))?$")
SOURCE_ID = re.compile(r"^src-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass
class Report:
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def error(self, rule: str, detail: str) -> None:
        self.errors.append((rule, detail))

    def warn(self, rule: str, detail: str) -> None:
        self.warnings.append((rule, detail))


class Validator:
    def __init__(self, root: Path, report: Report) -> None:
        self.root = root
        self.report = report
        self.strict = False
        self.course_cards: dict[str, set[str]] = {}
        self.cross_links: list[tuple[str, str, str]] = []

    def error(self, rule: str, at: str, detail: str) -> None:
        self.report.error(rule, f"{at}: {detail}")

    def relation(self, rule: str, at: str, detail: str) -> None:
        emit = self.report.error if self.strict else self.report.warn
        emit(rule, f"{at}: {detail}")

    def path(self, relative: str, at: str) -> Path | None:
        try:
            return safe_path(self.root, relative)
        except (DataError, OSError, ValueError) as exc:
            self.error("managed-path", at, str(exc))
            return None

    def document(self, relative: str) -> dict | None:
        path = self.path(relative, relative)
        if path is None:
            return None
        try:
            obj = load_json(path)
        except (DataError, OSError, ValueError) as exc:
            self.error("json-read", relative, str(exc))
            return None
        if not isinstance(obj, dict):
            self.error("object-type", relative, "JSON root must be an object")
            return None
        return obj

    def strings(self, obj: dict, key: str, at: str, required: bool = False) -> list[str]:
        result = []
        for index, value in enumerate(self.array(obj, key, at, required)):
            if not isinstance(value, str) or not value:
                self.error("string-type", f"{at}.{key}[{index}]", "must be a nonempty string")
            else:
                result.append(value)
        if len(result) != len(set(result)):
            self.error("duplicate-value", f"{at}.{key}", "contains duplicate strings")
        return result

    def array(self, obj: dict, key: str, at: str, required: bool = False) -> list:
        if key not in obj:
            if required:
                self.error("required", at, f"missing {key}")
            return []
        value = obj[key]
        if not isinstance(value, list):
            self.error("array-type", f"{at}.{key}", "must be an array")
            return []
        return value

    def objects(self, obj: dict, key: str, at: str, required: bool = False) -> list[tuple[str, dict]]:
        result = []
        for index, value in enumerate(self.array(obj, key, at, required)):
            here = f"{at}.{key}[{index}]"
            if not isinstance(value, dict):
                self.error("object-type", here, "must be an object")
            else:
                result.append((here, value))
        return result

    def string(self, obj: dict, key: str, at: str, required: bool = False, nullable: bool = False) -> str | None:
        if key not in obj:
            if required:
                self.error("required", at, f"missing {key}")
            return None
        value = obj[key]
        if nullable and value is None:
            return None
        if not isinstance(value, str) or not value:
            self.error("string-type", f"{at}.{key}", "must be a nonempty string")
            return None
        return value

    def integer(self, obj: dict, key: str, at: str, required: bool = False, minimum: int = 0, nullable: bool = False) -> int | None:
        if key not in obj:
            if required:
                self.error("required", at, f"missing {key}")
            return None
        value = obj[key]
        if nullable and value is None:
            return None
        if type(value) is not int or value < minimum:
            self.error("integer-type", f"{at}.{key}", f"must be an integer >= {minimum}")
            return None
        return value

    def pending(self, obj: dict, key: str, at: str) -> None:
        if key in obj and obj[key] is None:
            self.report.warn("metadata-pending", f"{at}.{key}: explicitly unknown; fill from source material when available")

    def date_interval(self, value: str, at: str) -> None:
        valid = bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} / [0-9]{4}-[0-9]{2}-[0-9]{2}", value))
        if valid:
            try:
                start, end = value.split(" / ")
                valid = date.fromisoformat(start) <= date.fromisoformat(end)
            except ValueError:
                valid = False
        if not valid:
            self.error("date-range-format", at, "must be a valid YYYY-MM-DD / YYYY-MM-DD interval in chronological order")

    def enum(self, obj: dict, key: str, values: set[str], at: str, required: bool = False) -> str | None:
        value = self.string(obj, key, at, required)
        if value is not None and value not in values:
            self.error("enum", f"{at}.{key}", f"unsupported value {value!r}")
        return value

    def timestamp(self, obj: dict, key: str, at: str, required: bool = False) -> None:
        value = self.string(obj, key, at, required)
        if value is None:
            return
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            valid = "T" in value and parsed.utcoffset() is not None
        except ValueError:
            valid = False
        if not valid:
            self.error("timestamp", f"{at}.{key}", "must be ISO 8601 with timezone")

    def raw_reference(self, directory: str, value: object, at: str) -> str | None:
        if not isinstance(value, str) or not value or Path(value).name != value or value in {".", ".."} or "\\" in value:
            self.error("source-filename", at, "must be a raw file basename")
            return None
        path = self.path(f"{directory}/raw/{value}", at)
        if path is not None and not path.is_file():
            self.error("source-missing", at, f"raw/{value} does not exist")
        return value

    def locator(self, obj: dict, at: str, required: bool = False) -> bool:
        if "locator" not in obj:
            if required:
                self.error("required", at, "missing locator")
            return False
        value = obj["locator"]
        if value is None:
            return False
        if not isinstance(value, dict):
            self.error("locator-type", at, "locator must be null or an object")
            return False
        before = len(self.report.errors)
        keys = {"pdf_page", "printed_page", "slide", "start_seconds", "end_seconds", "segment_id"}
        if not any(key in value for key in keys):
            self.error("locator-empty", at, "locator must contain at least one supported location")
        for key in sorted(set(value) - keys):
            self.error("locator-field", at, f"unsupported locator field {key!r}")
        for key in ("pdf_page", "slide", "start_seconds", "end_seconds"):
            self.integer(value, key, f"{at}.locator", minimum=1 if key in {"pdf_page", "slide"} else 0)
        for key in ("printed_page", "segment_id"):
            self.string(value, key, f"{at}.locator")
        start, end = value.get("start_seconds"), value.get("end_seconds")
        if type(start) is int and type(end) is int and end < start:
            self.error("locator-range", at, "end_seconds precedes start_seconds")
        return len(self.report.errors) == before

    def registry(self, directory: str, raw_files: set[str]) -> tuple[dict[str, dict], dict[str, dict]]:
        by_id: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        name = f"{directory}/sources.json"
        registry = self.document(name)
        if registry is None:
            return by_id, by_name
        if registry.get("schema_version") != "1.1":
            self.error("schema-version", name, "schema_version must be '1.1'")
        for at, source in self.objects(registry, "sources", name, True):
            sid = self.string(source, "source_id", at, True)
            filename = self.raw_reference(directory, source.get("filename"), f"{at}.filename")
            if sid is not None:
                if not SOURCE_ID.fullmatch(sid):
                    self.error("source-id", at, "source_id must be src-<UUID>")
                if sid in by_id:
                    self.error("source-id-duplicate", at, f"duplicate source_id {sid}")
                by_id[sid] = source
            if filename is not None:
                if filename in by_name:
                    self.error("source-filename-duplicate", at, f"duplicate filename {filename}")
                by_name[filename] = source
            self.enum(source, "kind", KINDS, at, True)
            self.enum(source, "generation_method", GENERATIONS, at, True)
            self.enum(source, "verification_status", VERIFICATIONS, at, True)
            self.string(source, "original_filename", at, True, True)
            self.string(source, "version_note", at, True, True)
            self.strings(source, "derived_from", at, True)
            checksum = self.string(source, "sha256", at, True)
            if checksum is not None and not re.fullmatch(r"[0-9a-f]{64}", checksum):
                self.error("source-hash", at, "sha256 must contain 64 lowercase hexadecimal characters")
            elif checksum is not None and filename is not None:
                path = self.path(f"{directory}/raw/{filename}", at)
                if path is not None and path.is_file():
                    try:
                        actual = sha256_file(path)
                    except OSError as exc:
                        self.error("source-hash", at, str(exc))
                    else:
                        if actual != checksum:
                            self.error("source-hash", at, f"raw/{filename} checksum differs from registry")
        for sid, source in by_id.items():
            for parent in self.strings(source, "derived_from", f"{name}:{sid}"):
                if parent == sid or parent not in by_id:
                    self.error("derived-from", f"{name}:{sid}", f"invalid local source reference {parent!r}")
        for filename in sorted(raw_files - set(by_name)):
            self.report.warn("raw-unregistered", f"{name}: raw/{filename} is not registered")
        return by_id, by_name

    def source_id(self, obj: dict, registry: dict[str, dict], at: str, filename: str | None = None) -> dict | None:
        sid = self.string(obj, "source_id", at, True)
        if sid is None:
            return None
        source = registry.get(sid)
        if source is None:
            self.error("source-id-dangling", at, f"source_id {sid!r} has no registry entry")
            return None
        if filename is not None and source.get("filename") != filename:
            self.error("source-id-filename", at, "source_id and filename refer to different sources")
        return source

    def slide_refs(self, week: dict, at: str) -> None:
        sources = [(i, s) for i, s in enumerate(week.get("sources", [])) if isinstance(s, dict)] if isinstance(week.get("sources"), list) else []
        slides = {s["filename"]: s for _, s in sources if s.get("type") == "slides" and isinstance(s.get("filename"), str)}
        for index, source in sources:
            source_at = f"{at}.sources[{index}]"
            for segment_at, segment in self.objects(source, "segments", source_at):
                for ref in self.strings(segment, "slide_refs", segment_at):
                    filename, page = ref.rsplit("#", 1) if "#" in ref else (None, ref)
                    match = PAGE_REF.fullmatch(page)
                    if match is None:
                        self.report.warn("slide-ref-format", f"{segment_at}: invalid legacy slide reference {ref!r}")
                        continue
                    if filename is not None and filename not in slides:
                        self.report.warn("slide-ref-source", f"{segment_at}: {filename!r} is not a slides source in this week")
                        continue
                    if filename is None and len(slides) > 1:
                        self.report.warn("slide-ref-ambiguous", f"{segment_at}: qualify {ref!r} with a slides filename")
                    first, last = int(match[1]), int(match[2] or match[1])
                    selected = slides.get(filename) if filename else next(iter(slides.values()), None) if len(slides) == 1 else None
                    count = selected.get("page_count") if selected else None
                    if first > last or (type(count) is int and last > count):
                        self.report.warn("slide-ref-range", f"{segment_at}: slide reference {ref!r} exceeds its page range")

    def course(self, code: str, directory: str, week_ranges: dict[str, str | None]) -> None:
        at = f"{directory}/course.json"
        course = self.document(at)
        if course is None:
            return
        for key in ("code", "name"):
            self.string(course, key, at, True)
        self.string(course, "instructor", at, True, True)
        self.pending(course, "instructor", at)
        if course.get("code") != code:
            self.error("course-code", at, "code differs from manifest entry")
        self.integer(course, "total_weeks", at, True, 1, nullable=True)
        self.pending(course, "total_weeks", at)
        indexed = self.integer(course, "weeks_indexed", at, True)
        language = self.string(course, "language", at, True, True)
        if language is not None and language not in {"en", "zh", "mixed"}:
            self.error("course-language", at, f"unsupported language {language!r}")
        self.timestamp(course, "created", at, True)
        self.timestamp(course, "last_updated", at)
        for key in ("schedule", "assessment", "contacts"):
            if key in course and not isinstance(course[key], dict):
                self.error("object-type", f"{at}.{key}", "must be an object")
        for key in ("schedule", "assessment"):
            if isinstance(course.get(key), dict):
                self.objects(course[key], "components", f"{at}.{key}", True)
        raw_dir = self.path(f"{directory}/raw", directory)
        raw_files = set()
        if raw_dir is None or not raw_dir.is_dir():
            self.error("raw-directory", directory, "raw directory is missing or invalid")
        else:
            for path in sorted(raw_dir.iterdir()):
                if path.name.startswith("."):
                    continue
                checked = self.path(f"{directory}/raw/{path.name}", str(path))
                if checked is not None and checked.is_file():
                    raw_files.add(path.name)
                elif checked is not None:
                    self.error("raw-file", directory, f"raw/{path.name} must be a regular file")
        registry, _ = self.registry(directory, raw_files) if self.strict else ({}, {})
        weekly_dir = self.path(f"{directory}/index/weekly", directory)
        concept_dir = self.path(f"{directory}/index/concepts", directory)
        weekly_paths = sorted(weekly_dir.glob("*.json")) if weekly_dir and weekly_dir.is_dir() else []
        concept_paths = sorted(concept_dir.glob("*.json")) if concept_dir and concept_dir.is_dir() else []
        if indexed != len(weekly_paths):
            self.error("weeks-indexed", at, f"weeks_indexed={indexed!r}, but {len(weekly_paths)} weekly files exist")
        weeks: dict[int, dict] = {}
        week_sources: dict[int, dict[str, dict]] = {}
        referenced_raw: set[str] = set()
        for path in weekly_paths:
            name = f"{directory}/index/weekly/{path.name}"
            week = self.document(name)
            if week is None:
                continue
            match = WEEK.fullmatch(path.stem)
            if match is None:
                self.error("weekly-name", name, "filename must be w<NN>.json")
            number = self.integer(week, "week", name, True)
            if number is not None:
                if match and number != int(match[1]):
                    self.error("weekly-number", name, "week differs from filename")
                if number in weeks:
                    self.error("weekly-duplicate", name, f"duplicate week {number}")
                weeks[number] = week
            if week.get("course") != code:
                self.error("weekly-course", name, "course differs from manifest entry")
            self.string(week, "topic", name, True, True)
            self.string(week, "weekly_summary", name, True, True)
            self.strings(week, "open_questions", name, True)
            self.strings(week, "key_concepts", name, True)
            self.enum(week, "status", {"complete", "partial", "pre-class"}, name)
            self.timestamp(week, "last_updated", name)
            actual_range = self.string(week, "date_range", name, True, True)
            self.pending(week, "date_range", name)
            if actual_range is not None:
                self.date_interval(actual_range, f"{name}.date_range")
            if week_ranges:
                if path.stem not in week_ranges:
                    self.error("weekly-calendar", name, "week is absent from calendar.week_ranges; preserve unknown week dates with an explicit null value")
                elif actual_range is not None and week_ranges[path.stem] is not None and actual_range != week_ranges[path.stem]:
                    self.error("date-range", name, "date_range differs from calendar week interval")
            if self.strict:
                self.enum(week, "indexing_status", {"complete", "partial"}, name, True)
                self.strings(week, "unresolved_concepts", name, True)
                availability = week.get("material_availability")
                if not isinstance(availability, dict):
                    self.error("object-type", name, "material_availability must be an object")
                else:
                    self.enum(availability, "status", {"unknown", "incomplete", "complete"}, f"{name}.material_availability", True)
                    self.strings(availability, "missing", f"{name}.material_availability", True)
            local_sources: dict[str, dict] = {}
            for source_at, source in self.objects(week, "sources", name, True):
                filename = self.raw_reference(directory, source.get("filename"), f"{source_at}.filename")
                self.enum(source, "type", KINDS, source_at, True)
                self.strings(source, "key_concepts", source_at, self.strict)
                self.integer(source, "page_count", source_at, minimum=1)
                if filename is not None:
                    if filename in local_sources:
                        self.error("weekly-source-duplicate", source_at, f"duplicate source {filename!r}")
                    local_sources[filename] = source
                    referenced_raw.add(filename)
                if self.strict:
                    registered = self.source_id(source, registry, source_at, filename)
                    if registered is not None and source.get("type") != registered.get("kind"):
                        self.error("source-kind", source_at, "type differs from source registry kind")
            if number is not None:
                week_sources[number] = local_sources
            self.slide_refs(week, name)
        cards: dict[str, dict] = {}
        expected_weeks: dict[int, set[str]] = {number: set() for number in weeks}
        expected_sources: dict[tuple[int, str], set[str]] = {}
        for path in concept_paths:
            name = f"{directory}/index/concepts/{path.name}"
            card = self.document(name)
            if card is None:
                continue
            slug = path.stem
            cards[slug] = card
            if not SLUG.fullmatch(slug) or card.get("slug") != slug:
                self.error("slug-canonical", name, "slug must match its lowercase hyphenated filename")
            self.string(card, "name", name, True)
            self.strings(card, "aliases", name, True)
            self.strings(card, "related", name, True)
            self.strings(card, "tags", name, True)
            self.string(card, "originator", name, True, True)
            definition = self.string(card, "definition", name, True, True)
            basis = card.get("definition_basis")
            if definition is None:
                if "definition_basis" in card:
                    self.error("definition-basis", name, "omit definition_basis when definition is null")
            else:
                self.enum(card, "definition_basis", {"verbatim", "synthesized", "unverified"} if self.strict else {"verbatim", "synthesized"}, name, True)
            appearances = self.objects(card, "appearances", name, True)
            if not appearances:
                self.relation("appearances-empty", name, "no appearances recorded")
            for appearance_at, appearance in appearances:
                number = self.integer(appearance, "week", appearance_at, True)
                filename = self.raw_reference(directory, appearance.get("source_file"), f"{appearance_at}.source_file")
                self.string(appearance, "context", appearance_at, True, True)
                if "coverage" in appearance:
                    self.enum(appearance, "coverage", COVERAGES, appearance_at)
                if self.strict:
                    self.source_id(appearance, registry, appearance_at, filename)
                    self.locator(appearance, appearance_at, True)
                    self.enum(appearance, "verification_status", VERIFICATIONS, appearance_at, True)
                    self.enum(appearance, "assertion_basis", ASSERTIONS, appearance_at, True)
                if number is not None:
                    if number not in weeks:
                        self.relation("appearance-week", appearance_at, f"week {number} has no weekly index")
                    else:
                        expected_weeks[number].add(slug)
                    if filename is not None:
                        expected_sources.setdefault((number, filename), set()).add(slug)
                        if filename not in week_sources.get(number, {}):
                            self.relation("appearance-source", appearance_at, "source is not registered in this weekly index")
            verified_quote = False
            verified_quote_texts: set[str] = set()
            for quote_at, quote in self.objects(card, "quotes", name, self.strict):
                text = self.string(quote, "text", quote_at, True)
                registered = self.source_id(quote, registry, quote_at) if self.strict else None
                located = self.locator(quote, quote_at, True)
                status = self.enum(quote, "verification_status", VERIFICATIONS, quote_at, True)
                if status == "verified" and (not located or not text or (self.strict and registered is None)):
                    self.error("quote-evidence", quote_at, "verified quote requires text, a source, and a valid locator")
                if status == "verified" and located and text and registered is not None:
                    verified_quote = True
                    verified_quote_texts.add(text)
            if self.strict and basis == "verbatim" and not verified_quote:
                self.error("verbatim-evidence", name, "verbatim definition requires a verified quote with a source and locator")
            elif self.strict and basis == "verbatim" and definition not in verified_quote_texts:
                self.report.warn("definition-quote-review", f"{name}: definition does not exactly match any verified quote text; review wording and attribution")
            for pointer_at, pointer in self.objects(card, "cross_course", name):
                other = self.string(pointer, "course", pointer_at, True)
                target = self.string(pointer, "slug", pointer_at, True)
                self.string(pointer, "note", pointer_at, True)
                if other is not None and target is not None:
                    self.cross_links.append((pointer_at, other, target))
        self.course_cards[code] = set(cards)
        for slug, card in sorted(cards.items()):
            for related in self.strings(card, "related", f"{directory}/index/concepts/{slug}.json"):
                if related not in cards:
                    self.relation("related-dangling", f"{code}/{slug}", f"related concept {related!r} is missing")
        all_referenced: set[str] = set()
        for number, week in sorted(weeks.items()):
            name = f"{directory}/index/weekly/w{number:02}.json"
            actual = set(self.strings(week, "key_concepts", name))
            unresolved = set(self.strings(week, "unresolved_concepts", name)) if self.strict else set()
            expected = expected_weeks[number] | unresolved
            all_referenced.update(actual)
            if actual != expected:
                self.relation("weekly-concepts-derived", name, f"key_concepts differs from appearances/unresolved: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")
            for slug in sorted(actual | unresolved):
                if slug not in cards:
                    self.relation("weekly-concept-dangling", name, f"concept {slug!r} has no card")
            for filename, source in sorted(week_sources[number].items()):
                source_at = f"{name}:{filename}"
                actual_source = set(self.strings(source, "key_concepts", source_at))
                expected_source = expected_sources.get((number, filename), set())
                if actual_source != expected_source:
                    self.relation("source-concepts-derived", source_at, f"key_concepts differs from appearances: missing={sorted(expected_source-actual_source)}, extra={sorted(actual_source-expected_source)}")
                for slug in sorted(actual_source - set(cards)):
                    self.relation("source-concept-dangling", source_at, f"concept {slug!r} has no card")
        for slug in sorted(set(cards) - all_referenced):
            self.relation("orphan-card", f"{code}/{slug}", "card never appears in weekly key_concepts")
        if not self.strict:
            course_blob = json.dumps(course, ensure_ascii=False)
            for filename in sorted(raw_files - referenced_raw):
                if not filename.lower().startswith("syllabus") and filename not in course_blob:
                    self.report.warn("raw-unindexed", f"{code}: raw/{filename} has no weekly or course reference")

    def run(self) -> None:
        manifest = self.document("manifest.json")
        if manifest is None:
            return
        version = self.string(manifest, "kb_version", "manifest.json", True)
        if version not in {"1.0", "1.1"}:
            self.error("schema-version", "manifest.json", f"unsupported kb_version {version!r}")
            return
        self.strict = version == "1.1"
        for key in ("owner", "semester", "institution", "program"):
            self.string(manifest, key, "manifest.json", True, nullable=True)
            if key != "owner":
                self.pending(manifest, key, "manifest.json")
        self.timestamp(manifest, "last_updated", "manifest.json", True)
        calendar = manifest.get("calendar")
        week_ranges: dict[str, str | None] = {}
        if not isinstance(calendar, dict):
            self.error("calendar", "manifest.json", "calendar must be an object")
        else:
            for key in ("source", "term"):
                self.string(calendar, key, "manifest.calendar", True, True)
                self.pending(calendar, key, "manifest.calendar")
            self.integer(calendar, "teaching_weeks", "manifest.calendar", True, 1, nullable=True)
            self.pending(calendar, "teaching_weeks", "manifest.calendar")
            ranges = calendar.get("week_ranges")
            if not isinstance(ranges, dict):
                self.error("calendar", "manifest.calendar", "week_ranges must be an object; use {} while the calendar is unknown")
            elif not ranges:
                self.report.warn("calendar-pending", "manifest.calendar.week_ranges: empty; week membership and dates cannot be checked yet")
            else:
                for key, value in sorted(ranges.items()):
                    if not WEEK.fullmatch(key) or (value is not None and not isinstance(value, str)):
                        self.error("calendar-range", "manifest.calendar", f"invalid week range {key!r}: {value!r}")
                        continue
                    if value is None:
                        self.report.warn("calendar-pending", f"manifest.calendar.week_ranges.{key}: week is known but dates are unknown")
                    else:
                        self.date_interval(value, f"manifest.calendar.week_ranges.{key}")
                    week_ranges[key] = value
        codes: set[str] = set()
        directories: set[str] = set()
        for at, entry in self.objects(manifest, "courses", "manifest.json", True):
            code = self.string(entry, "code", at, True)
            directory = self.string(entry, "directory", at, True)
            self.string(entry, "name", at, True)
            if code is None or directory is None:
                continue
            if code in codes or directory in directories:
                self.error("manifest-course-duplicate", at, "duplicate code or directory")
                continue
            codes.add(code)
            directories.add(directory)
            path = self.path(directory, at)
            if path is None:
                continue
            if not path.is_dir():
                self.error("course-directory", at, "course directory does not exist")
                continue
            self.course(code, directory, week_ranges)
        for at, code, slug in self.cross_links:
            if slug not in self.course_cards.get(code, set()):
                self.relation("cross-course", at, f"target {code}/{slug} is missing")


def validate(root: Path | str, check_runtime: bool = True) -> Report:
    """始终返回报告；损坏数据不应截断整个命令的 JSON 输出。"""
    report = Report()
    try:
        path = Path(root).expanduser().resolve()
        if not path.is_dir():
            report.error("root", f"root is not an existing directory: {path}")
            return report
        if check_runtime:
            assert_readable(path)
        Validator(path, report).run()
    except (DataError, OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError) as exc:
        report.error("validation-failure", f"{type(exc).__name__}: {exc}")
    report.errors.sort()
    report.warnings.sort()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate course-kb schemas and source integrity.")
    parser.add_argument("--root", default=str(Path.home() / "course-kb"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = validate(args.root)
    if args.json:
        print(json.dumps({"root": str(Path(args.root).expanduser().absolute()), "errors": report.errors, "warnings": report.warnings}, ensure_ascii=False, indent=2))
    else:
        for rule, detail in report.errors:
            print(f"ERROR [{rule}] {detail}")
        for rule, detail in report.warnings:
            print(f"WARN  [{rule}] {detail}")
        print(f"{'FAIL' if report.errors else 'PASS'}: {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 1 if report.errors else 0
