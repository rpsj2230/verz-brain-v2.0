"""Unpacking a skill archive, which is where a validated name becomes a written file.

`brain.tools.skills.safe_archive_members` refuses names that could escape the extraction
root, and it says plainly what it cannot check: a member's *mode*. A symlink pointing at
`/etc` has a perfectly ordinary name, and no rule about names will ever catch it. This is
the other half - the part that opens the archive and therefore can see modes, sizes and
what the paths actually resolve to once joined.

Four things are refused here that a name check cannot see.

**Anything that is not a regular file or a directory.** A symlink, a hard link, a device
node, a fifo. A symlink is the interesting one: extract `link -> /etc/passwd` and then a
later member called `link` writes through it, so two individually harmless members compose
into a write outside the root. Python's own `tarfile` had this as a CVE, and the fix in the
standard library is a filter that must be asked for by name.

**A resolved path outside the root.** The name check is the first line and this is the one
that cannot be argued with: after joining and resolving, the destination is either under the
root or it is not. Belt and braces on purpose, because the name check is a set of rules
about strings and this is the actual question.

**An archive larger than the budget, decompressed.** A few hundred kilobytes of zip can be
a terabyte of zeroes. The declared size is read *before* extracting anything, and the
running total is checked as members are written, because a declared size is a claim the
archive makes about itself.

**Too many members.** A hundred thousand empty files is not a decompression bomb by size
and will still exhaust the inodes on whatever volume this lands on.

Task ids: M12.2.4
"""

from __future__ import annotations

import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from brain.tools.skills import SkillError, safe_archive_members

#: Total uncompressed bytes a skill archive may hold. A skill is instructions and a few
#: scripts; anything approaching this is not one, whatever it says it is.
MAX_UNCOMPRESSED = 8 * 1024 * 1024

#: How many members. A hundred thousand empty files exhausts inodes without ever
#: approaching the byte budget, so the two limits catch different attacks.
MAX_MEMBERS = 500


@dataclass(frozen=True)
class Extracted:
    """Where a skill was unpacked, and what was written.

    `written` is relative paths, so it can be logged and compared without leaking the
    temporary directory's name into anything.
    """

    root: Path
    written: tuple[str, ...]
    total_bytes: int


def _assert_inside(root: Path, candidate: Path) -> None:
    """The check that cannot be argued with, after joining and resolving.

    `safe_archive_member` reasons about strings and catches the cases worth naming in an
    error message. This asks the question the filesystem will actually answer, and it is
    the one that survives a name nobody thought of - a normalisation the string check does
    not perform, a symlink already in the root from an earlier member, a platform joining
    paths differently from the one this was written on.
    """
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        msg = "an archive member resolved outside the extraction root"
        raise SkillError(msg)


def extract_zip(archive: Path, root: Path) -> Extracted:
    """Unpack a zip into `root`, refusing anything a name check cannot see."""
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            msg = f"this archive has {len(infos)} members, over the {MAX_MEMBERS} limit"
            raise SkillError(msg)

        # Declared sizes first, before a single byte is written. A declared size is a claim
        # the archive makes about itself and is checked again below as members are written,
        # because an archive that lies about its size is exactly the archive to worry about.
        declared = sum(info.file_size for info in infos)
        if declared > MAX_UNCOMPRESSED:
            msg = f"this archive declares {declared} bytes, over the {MAX_UNCOMPRESSED} limit"
            raise SkillError(msg)

        names = safe_archive_members(info.filename for info in infos)
        del names  # Raises on a bad archive; the checked names are the same strings.

        root.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            # `create_system` and the external attribute high bits carry the unix mode. A
            # zip can hold a symlink and most extractors write it as one.
            mode = info.external_attr >> 16
            if mode and not _is_regular(mode):
                msg = f"archive member {info.filename!r} is not a regular file"
                raise SkillError(msg)

            destination = root / info.filename
            _assert_inside(root, Path(info.filename))
            destination.parent.mkdir(parents=True, exist_ok=True)

            with zf.open(info) as source:
                data = source.read(MAX_UNCOMPRESSED - total + 1)
            total += len(data)
            if total > MAX_UNCOMPRESSED:
                # Reached only when the declared size lied, which is the case the first
                # check cannot cover.
                msg = "this archive is larger than it declared"
                raise SkillError(msg)
            destination.write_bytes(data)
            written.append(info.filename)

    return Extracted(root=root, written=tuple(written), total_bytes=total)


def _is_regular(mode: int) -> bool:
    """Whether a unix mode says regular file, directory, or nothing at all.

    `0o100000` is a regular file and `0o040000` a directory. A symlink is `0o120000`, a
    device `0o020000` or `0o060000`, a fifo `0o010000`, a socket `0o140000`. Listed as what
    is allowed rather than what is refused, for the reason the audit redactor gives at
    greater length: a denylist of dangerous kinds is a list somebody has to keep complete.

    **Type bits of zero mean unspecified, and unspecified is a regular file.** Most zip
    writers do not record a unix type at all - `zipfile.writestr` sets permissions and
    leaves the type empty, and so does every zip produced on Windows. Treating that as "not
    a regular file" refuses almost every real archive, which is how this check gets deleted
    rather than fixed. Found by the first test written against a real zip.

    The consequence is worth being clear about: a zip that records no type cannot be
    checked this way, and the defence for it is `_assert_inside` plus the name rules. That
    is why those exist separately rather than as belt and braces on this one.
    """
    kind = mode & 0o170000
    return kind in (0, 0o100000, 0o040000)


def extract_tar(archive: Path, root: Path) -> Extracted:
    """Unpack a tar into `root`. Same refusals as the zip path, different member metadata.

    Uses `filter="data"`, which is the standard library's own refusal of links, devices and
    absolute paths - added in 3.12 after this class of bug turned out to be endemic. It is
    asked for explicitly rather than relied on as a default, because the default changed
    between versions and a security property that depends on which Python is installed is
    not a property.
    """
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        if len(members) > MAX_MEMBERS:
            msg = f"this archive has {len(members)} members, over the {MAX_MEMBERS} limit"
            raise SkillError(msg)

        declared = sum(m.size for m in members)
        if declared > MAX_UNCOMPRESSED:
            msg = f"this archive declares {declared} bytes, over the {MAX_UNCOMPRESSED} limit"
            raise SkillError(msg)

        for member in members:
            if not (member.isfile() or member.isdir()):
                # Named rather than silently skipped: an archive containing a symlink is an
                # archive somebody built to contain one.
                msg = f"archive member {member.name!r} is not a regular file"
                raise SkillError(msg)

        safe_archive_members(m.name for m in members)
        for member in members:
            _assert_inside(root, Path(member.name))

        root.mkdir(parents=True, exist_ok=True)
        tf.extractall(root, filter="data")

    written = tuple(sorted(m.name for m in members if m.isfile()))
    return Extracted(root=root, written=written, total_bytes=declared)
