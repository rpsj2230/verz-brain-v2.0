"""An audit export a third party can verify without running any of our code.

**What breaks without it.** The ledger's guarantee is real and unusable. `AuditChain.verify`
proves the chain holds, but it proves it to us, using our code, over our database, at a
moment we chose. A regulator asked to accept that has been asked to trust the party under
investigation. A court asked to accept it has been handed hearsay with a hash on it. The
whole point of a hash chain is that the proof is checkable by someone who trusts nothing,
and a chain nobody outside can check is an expensive way of promising to be honest.

So the export is not a dump of the table. It is the table *plus the recipe*, and the recipe
is the part a CSV can never carry:

- **The digests and the links**, exactly as stored, so recomputation has something to
  disagree with.
- **The hash recipe in full** - the schema label, the ordered list of hashed fields, the
  length-prefix encoding, the UTC normalisation, the sorting of details, the hash function
  and the genesis value. With these, a verifier is about twenty lines in any language. A
  CSV of entries and digests is not verifiable at all: the reader cannot know what was
  hashed, in what order, or how the parts were joined, and a wrong guess is
  indistinguishable from a forged entry.
- **The window**, stated as a start hash and a sequence range, because a run of entries
  taken out of a longer ledger has to be verifiable on its own or it looks like a forgery.
- **The external anchors**, and a plain statement of what the chain does not prove. A
  document that says "verified" without saying that a hash chain cannot see a truncated
  tail invites a reader to believe more than the evidence supports, and an export that
  overstates its own strength is worse in court than one that claims nothing.

Two refusals shape the rest of the module.

**A filtered export is refused.** "Every entry about this employee" is not a chain: remove
the entries in between and the links no longer meet, so the artefact either fails
verification or has to be shipped with the verification disabled, and an unverifiable
document that looks like a verified one is the worst possible thing to hand a regulator.
The export therefore covers a contiguous window or nothing. A filtered *view* is a
different artefact with a different name (M24.1.5), and `cite_entry` is how one entry from
it is tied back to a verifiable export.

**A broken chain still exports.** Refusing to export a ledger that fails verification would
mean the one circumstance where an external copy matters most is the one where it cannot be
produced. The manifest carries the break instead, so the document says "here is the ledger
and here is where it stopped holding" rather than not existing.

Nothing here relaxes what the ledger refused to store. Entries are exported verbatim,
which is safe precisely because `AuditEntry` already refuses to hold a value; everything
this module *adds* - the reason an export was taken, who took it, which anchors were
cited - is checked against the same field-name and identifier grammars, because a document
assembled for a regulator is exactly the place a helpful engineer would attach a note
saying which employee it concerns.

Task ids: M24.1.6
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain.audit.ledger import (
    DIGEST,
    DIGEST_CHARS,
    FIELD_NAME,
    GENESIS_HASH,
    HASH_SCHEMA,
    IDENTIFIER,
    TRACE_ID,
    AuditChain,
    AuditEntry,
    ChainBreak,
)

#: The format label, carried in the manifest and versioned separately from `HASH_SCHEMA`.
#: The two change for different reasons: the hash schema changes only in a migration that
#: rewrites every digest, while the document layout can gain a field without any historical
#: entry ceasing to verify. One constant for both would tie a cosmetic change to a
#: catastrophic one.
EXPORT_FORMAT: Final = "brain.audit.export.v1"

#: The ordered field names `brain.audit.ledger.compute_entry_hash` feeds into the digest,
#: before the details pairs.
#:
#: Restated here rather than imported, because ledger.py does not export the order as data
#: and this module may not edit it. That duplication is a real risk and is closed by a
#: test rather than by a comment: `test_a_third_party_can_recompute_every_digest_from_the_
#: recipe_alone` implements the verifier a stranger would write from this list and asserts
#: it reproduces the ledger's own digests. Reorder either side and that test fails.
HASHED_FIELDS: Final[tuple[str, ...]] = (
    "hash_schema",
    "prev_hash",
    "seq",
    "at",
    "actor_id",
    "action",
    "subject",
    "ent_hash",
    "trace_id",
)

#: How the parts are turned into bytes. Prose, not code, because the reader of an export
#: has no code of ours to run - which is the entire premise of the document.
PART_ENCODING: Final = (
    "each part is encoded as its character length in decimal, a colon, then the part "
    "itself; the encoded parts are concatenated with no separator and the result is "
    "hashed as UTF-8. The part named hash_schema takes its value from this recipe; "
    "every other named part takes its value from the entry, as a decimal string where "
    "the entry holds a number"
)

TIMESTAMP_RULE: Final = (
    "converted to UTC and formatted as ISO 8601 with an offset, so the same instant "
    "written in two timezones digests identically"
)

DETAILS_RULE: Final = (
    "after the fields above, the details mapping is appended in ascending key order, "
    "each pair as two further parts: the key, then the value"
)

#: What the chain provably does not show. Fixed strings, carried in every export, because a
#: reader who is told a document is "verified" and not told the limits of the verification
#: has been misled by omission. The wording follows the ledger's own module docstring.
LIMITATIONS: Final[tuple[str, ...]] = (
    "a hash chain proves continuity, not completeness: deleting the newest entries "
    "leaves a shorter chain that verifies perfectly, and only an anchor recorded outside "
    "this system can show that it happened",
    "anyone able to rewrite every entry from a tamper point forward can produce a chain "
    "that verifies; the chain proves nothing was quietly edited, not that nothing was "
    "edited",
    "the window below verifies against its start hash, which is evidence about this run "
    "of entries and says nothing about entries before it",
)


#: What `ChainBreak.expected` and `ChainBreak.actual` are allowed to be: a chain digest or a
#: decimal sequence number. The two shapes the ledger's walk actually produces.
_DIGEST_OR_NUMBER_RE: Final = re.compile(r"^(?:[0-9a-f]{64}|[0-9]{1,19})$")


class ExportRefusedError(Exception):
    """The export would have produced a document that cannot be verified.

    Deliberately not part of the user-facing taxonomy in `brain.core.errors`. Nobody asking
    a question ever sees this: it is raised at the moment a compliance officer asks for an
    artefact the format cannot honestly produce, and the right outcome is a refusal with a
    reason rather than a document with the verification quietly switched off.
    """


class Anchor(BaseModel):
    """A digest recorded somewhere the database administrator does not control.

    The only thing that closes the truncation hole, so an export that cites none says so
    rather than letting the absence pass unnoticed. `recorded_by` is an identifier and
    `where` is a field-name token (`offsite_object_store`, `notarised_email`) rather than
    free text, for the reason `LegalHold.reason_code` gives: an anchor description is
    exactly where somebody would eventually write which dispute it was taken for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0)
    entry_hash: str = Field(pattern=DIGEST)
    recorded_at: datetime
    recorded_by: str = Field(pattern=IDENTIFIER)
    where: str = Field(pattern=FIELD_NAME, max_length=80)

    @field_validator("recorded_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "an anchor timestamp must be timezone-aware; a naive one is a silent bug"
            raise ValueError(msg)
        return v


class HashRecipe(BaseModel):
    """Everything needed to recompute a digest without our code.

    A separate model rather than loose keys in the manifest, so that the question "can a
    stranger verify this" is answered by one object that is either present and complete or
    absent, rather than by counting fields in a dictionary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hash_function: str = "sha256"
    hash_schema: str = HASH_SCHEMA
    fields_in_order: tuple[str, ...] = HASHED_FIELDS
    part_encoding: str = PART_ENCODING
    timestamp: str = TIMESTAMP_RULE
    details: str = DETAILS_RULE
    genesis: str = GENESIS_HASH
    digest_chars: int = DIGEST_CHARS


class ExportManifest(BaseModel):
    """What the document is, what it covers, and how much it proves.

    `reason_code` is a field-name token and not prose, and that is the single most
    load-bearing validator in this file. An export is requested for a reason, the reason is
    always about a person or a dispute, and a free-text field on the cover page of a
    document assembled for an outside reader is where the name of the complainant ends up.
    A token (`regulator_request`, `pending_litigation`) says as much as the register needs
    and nothing the ledger itself would have refused.

    `entries_digest` covers the entry lines and not this object, so a reader can check it
    with a standard tool over the tail of the file rather than having to reconstruct any
    part of our serialisation. See `render_export`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: str = Field(default=EXPORT_FORMAT, pattern=FIELD_NAME)
    exported_at: datetime
    exported_by: str = Field(pattern=IDENTIFIER)
    trace_id: str = Field(pattern=TRACE_ID)
    reason_code: str = Field(pattern=FIELD_NAME, max_length=80)

    #: The window. `start_hash` is what the first entry's prev_hash must equal, which is
    #: what lets a slice of a longer ledger verify on its own.
    start_hash: str = Field(pattern=DIGEST)
    entry_count: int = Field(ge=0)
    first_seq: int | None = None
    last_seq: int | None = None
    head: str = Field(pattern=DIGEST)

    #: sha256 over the entry block exactly as written, so the document is tamper-evident in
    #: transit independently of the chain inside it.
    entries_digest: str = Field(pattern=DIGEST)

    verified: bool
    #: Present exactly when `verified` is false. An export of a damaged ledger is the one
    #: that matters most, so the break travels in the document rather than blocking it.
    break_found: ChainBreak | None = None

    anchors: tuple[Anchor, ...] = ()
    recipe: HashRecipe = Field(default_factory=HashRecipe)
    limitations: tuple[str, ...] = LIMITATIONS

    @field_validator("exported_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            msg = "exported_at must be timezone-aware; a naive timestamp is a silent bug"
            raise ValueError(msg)
        return v

    @field_validator("recipe")
    @classmethod
    def _recipe_is_the_recipe(cls, v: HashRecipe) -> HashRecipe:
        """Pinned to the module constants, so the two prose fields on this document cannot
        be written by a caller.

        Every other string on the manifest is pattern-constrained, which leaves the recipe
        and the limitations as the only places prose could enter. They describe the format
        rather than this particular export, so there is no legitimate reason for a caller to
        supply them, and a field with no legitimate caller-supplied value should not accept
        one. A new format version changes the constants.
        """
        if v != HashRecipe():
            msg = "the recipe describes the format, not this export; it cannot be overridden"
            raise ValueError(msg)
        return v

    @field_validator("limitations")
    @classmethod
    def _limitations_are_the_limitations(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if v != LIMITATIONS:
            msg = "the limitations describe the format, not this export; they are fixed"
            raise ValueError(msg)
        return v

    @field_validator("break_found")
    @classmethod
    def _break_is_a_reference(cls, v: ChainBreak | None) -> ChainBreak | None:
        """A break carries a digest or a sequence number, and this checks it.

        `ChainBreak.expected` and `ChainBreak.actual` are unconstrained strings in the
        ledger, which is right there: they are produced by the chain walk and never by a
        caller. Here they are, and a manifest can be constructed by hand, so they are the
        one route by which prose could reach a document assembled for an outside reader.
        Checked at the boundary rather than by widening the ledger, which this change may
        not edit.
        """
        if v is None:
            return v
        bad = [s for s in (v.expected, v.actual) if not _DIGEST_OR_NUMBER_RE.match(s)]
        if bad:
            msg = f"a chain break carries a digest or a sequence number, not {bad}"
            raise ValueError(msg)
        return v

    def model_post_init(self, _context: object, /) -> None:
        if self.verified != (self.break_found is None):
            # The failure this prevents: a document that says verified and carries a break,
            # or says broken and names nothing. Either one is read by whoever receives it as
            # the opposite of what the exporter meant.
            msg = "verified and break_found disagree; the manifest would mislead its reader"
            raise ValueError(msg)

    @property
    def anchored(self) -> bool:
        """Whether any anchor was cited. False means the truncation hole is open, which is
        a fact about this document and not a reason to withhold it."""
        return bool(self.anchors)


class AuditExport(BaseModel):
    """The manifest and the entries it describes, kept together in memory.

    `render` is the artefact; this is the object it is built from, so a caller can inspect
    what is about to leave before it does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: ExportManifest
    entries: tuple[AuditEntry, ...]

    def render(self) -> str:
        """The document. See `render_export` for the layout and why it is that layout."""
        return _manifest_line(self.manifest) + _entry_block(self.entries)

    def cite_entry(self, seq: int) -> str:
        """A one-line citation tying a single entry to this export.

        This is how a filtered view (M24.1.5) refers to something verifiable. The view
        itself cannot be self-verifying, so a row in it carries a citation of the form
        `<format> <entries_digest> seq=<n> entry_hash=<digest>`: the reader takes the
        verifiable export, finds that sequence number and compares the digest.

        Deliberately not a Merkle inclusion proof. That would let a single entry be proven
        without disclosing its neighbours, which is a genuinely better answer for a
        subject access request - and it is a second hashing construction, with its own
        schema, its own domain separation and its own migration story, on top of one that
        already exists. It is the right next step and it is not this leaf.
        """
        for entry in self.entries:
            if entry.seq == seq:
                return (
                    f"{self.manifest.format} {self.manifest.entries_digest} "
                    f"seq={entry.seq} entry_hash={entry.entry_hash}"
                )
        msg = f"seq {seq} is not in this export; a citation must point at something"
        raise ExportRefusedError(msg)


# ------------------------------------------------------------------ serialisation


def canonical_json(value: object) -> str:
    """One line, sorted keys, no incidental whitespace, UTF-8 preserved.

    Every byte of the entry block goes through here, because `entries_digest` is only
    meaningful if two runs over the same entries produce the same bytes. `sort_keys` rather
    than field order for the same reason `compute_entry_hash` sorts its details: insertion
    order is not part of a mapping's meaning, and two exports of the same ledger must not
    differ because a pydantic field moved.

    `ensure_ascii=False` on purpose. Escaping non-ASCII would still be deterministic, and it
    would also silently mangle a display label that a reader is entitled to see as it was
    written. The digest is taken over UTF-8 bytes, which the recipe states.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _entry_payload(entry: AuditEntry) -> dict[str, object]:
    """One entry as plain JSON types.

    `at` is written in UTC rather than as given, so the exported timestamp is the same
    string the digest was taken over. An export whose visible timestamp differs from the
    hashed one hands a reader an apparent contradiction and no way to resolve it.
    """
    return {
        "seq": entry.seq,
        "at": entry.at.astimezone(UTC).isoformat(),
        "actor_id": entry.actor_id,
        "action": entry.action.value,
        "subject": entry.subject,
        "ent_hash": entry.ent_hash,
        "trace_id": entry.trace_id,
        "details": dict(entry.details),
        "prev_hash": entry.prev_hash,
        "entry_hash": entry.entry_hash,
    }


def _entry_block(entries: Sequence[AuditEntry]) -> str:
    """The entry lines, each terminated by a newline. Digested exactly as written."""
    return "".join(canonical_json(_entry_payload(e)) + "\n" for e in entries)


def _manifest_line(manifest: ExportManifest) -> str:
    return canonical_json(manifest.model_dump(mode="json")) + "\n"


def entries_digest(entries: Sequence[AuditEntry]) -> str:
    """sha256 over the entry block.

    A plain digest of the bytes, with no length prefixing and no domain separation, and
    that is the point: the reader must be able to reproduce it with `sha256sum` over the
    file minus its first line. A cleverer construction would be marginally safer against an
    ambiguity that cannot arise here - the lines are newline-terminated JSON, so no run of
    entries encodes as another - and would cost the one property that makes the number
    worth carrying.
    """
    return hashlib.sha256(_entry_block(entries).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- the export


def build_export(
    chain: AuditChain,
    *,
    exported_by: str,
    trace_id: str,
    reason_code: str,
    at: datetime,
    anchors: Iterable[Anchor] = (),
) -> AuditExport:
    """Assemble an export of a whole chain, verified or not.

    `at` has no default for the reason `AuditChain.append` gives about its own timestamp:
    `datetime.now()` is the clock of whichever container happened to serve the request, and
    an export is a document whose date is later read out in evidence.

    The chain is verified here rather than by the caller, so that no code path exists which
    produces a manifest claiming verification that nobody performed.
    """
    entries = chain.entries
    _refuse_if_not_contiguous(entries)
    found = chain.first_break()
    manifest = ExportManifest(
        exported_at=at,
        exported_by=exported_by,
        trace_id=trace_id,
        reason_code=reason_code,
        start_hash=chain.start_hash,
        entry_count=len(entries),
        first_seq=entries[0].seq if entries else None,
        last_seq=entries[-1].seq if entries else None,
        head=chain.head(),
        entries_digest=entries_digest(entries),
        verified=found is None,
        break_found=found,
        anchors=tuple(anchors),
    )
    return AuditExport(manifest=manifest, entries=entries)


def render_export(
    chain: AuditChain,
    *,
    exported_by: str,
    trace_id: str,
    reason_code: str,
    at: datetime,
    anchors: Iterable[Anchor] = (),
) -> str:
    """The document: one manifest line, then one line per entry, oldest first.

    Line-delimited JSON rather than a single JSON object, and the reason is the reader
    rather than the writer. A ledger window is millions of lines; a single document has to
    be parsed whole by whatever tool opens it, while a line-delimited one streams, greps,
    diffs and splits with tools a regulator's analyst already has. It also makes
    `entries_digest` checkable in one shell command:

        tail -n +2 export.jsonl | sha256sum

    which is the difference between a number a reader can confirm and a number they have to
    take on trust.

    Rejected: CSV, which cannot carry the details mapping without inventing a nested
    encoding and cannot carry the recipe at all; and a signed PDF, which is a picture of the
    evidence rather than the evidence, and which nobody can recompute a digest from.
    """
    return build_export(
        chain,
        exported_by=exported_by,
        trace_id=trace_id,
        reason_code=reason_code,
        at=at,
        anchors=anchors,
    ).render()


def _refuse_if_not_contiguous(entries: Sequence[AuditEntry]) -> None:
    """Refuse a window whose sequence numbers have gaps.

    A caller who has filtered the entries has produced something that cannot verify, and
    the failure would otherwise surface as a manifest saying `verified: false` - which
    reads as "the ledger was tampered with" rather than "you asked for the wrong shape".
    Two very different findings, and the second one must not be delivered wearing the
    first one's clothes.
    """
    for previous, current in pairwise(entries):
        if current.seq != previous.seq + 1:
            msg = (
                f"the window skips from seq {previous.seq} to {current.seq}; a filtered "
                "selection is not a chain and cannot be exported as one. Export the "
                "contiguous window and cite the entries you need."
            )
            raise ExportRefusedError(msg)


# ----------------------------------------------------------------- verification


def verify_document(document: str) -> tuple[bool, str]:
    """Re-read a rendered export and check it against its own manifest.

    Not the verifier a third party writes - that one implements the recipe from scratch,
    which is the point of shipping the recipe - but the one *we* run before handing the
    document over, so that a truncated write or a mangled encoding is caught by the sender
    rather than discovered by the recipient. Returns the verdict and a reason, rather than
    raising, because "this document is damaged" is a finding to be reported and not an
    exception to be swallowed by whatever wrapped the call.
    """
    lines = document.splitlines(keepends=True)
    if not lines:
        return False, "empty document"
    try:
        manifest = ExportManifest.model_validate_json(lines[0])
    except ValueError as exc:
        return False, f"manifest line does not parse: {exc}"

    block = "".join(lines[1:])
    if hashlib.sha256(block.encode("utf-8")).hexdigest() != manifest.entries_digest:
        return False, "entries_digest does not match the entry block"
    if len(lines) - 1 != manifest.entry_count:
        return False, f"manifest claims {manifest.entry_count} entries, found {len(lines) - 1}"
    return True, "manifest matches the entry block"
