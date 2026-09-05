"""Unpacking a skill archive. Every test is a way a file lands outside the root, or a way
a small archive becomes a large one.

Real archives written to a real temporary directory, because the whole point of this module
is what the filesystem does with a name once it is joined and resolved. A fake would only
prove the fake behaves.

Task ids: M12.2.4
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

from brain.tools.extract import (
    MAX_MEMBERS,
    MAX_UNCOMPRESSED,
    extract_tar,
    extract_zip,
)
from brain.tools.skills import SkillError

SKILL_MD = b"---\nname: hosting-expiry\ndescription: check expiry\n---\n\n1. do the thing\n"


def _zip(tmp_path: Path, members: dict[str, bytes], name: str = "skill.zip") -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    return archive


def _tar(tmp_path: Path, members: dict[str, bytes], name: str = "skill.tar") -> Path:
    archive = tmp_path / name
    with tarfile.open(archive, "w") as tf:
        for member, data in members.items():
            source = tmp_path / "staging" / member
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)
            tf.add(source, arcname=member)
    return archive


# ------------------------------------------------------------------ the happy path
def test_a_well_formed_archive_unpacks(tmp_path: Path) -> None:
    """If this fails, every refusal below passes for the wrong reason: an extractor that
    refuses everything satisfies every other test in this file."""
    archive = _zip(tmp_path, {"SKILL.md": SKILL_MD, "scripts/check.py": b"print(1)\n"})
    result = extract_zip(archive, tmp_path / "out")
    assert set(result.written) == {"SKILL.md", "scripts/check.py"}
    assert (tmp_path / "out" / "SKILL.md").read_bytes() == SKILL_MD


def test_a_tar_unpacks_too(tmp_path: Path) -> None:
    archive = _tar(tmp_path, {"SKILL.md": SKILL_MD})
    result = extract_tar(archive, tmp_path / "out")
    assert result.written == ("SKILL.md",)


# ------------------------------------------------------------------- getting out
def test_a_traversing_member_is_refused(tmp_path: Path) -> None:
    """The classic, and the least interesting one - but an extractor that missed it is not
    worth reading further."""
    archive = _zip(tmp_path, {"SKILL.md": SKILL_MD, "../escaped.txt": b"x"})
    with pytest.raises(SkillError):
        extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_an_absolute_member_is_refused(tmp_path: Path) -> None:
    archive = _zip(tmp_path, {"SKILL.md": SKILL_MD, "/etc/passwd": b"x"})
    with pytest.raises(SkillError):
        extract_zip(archive, tmp_path / "out")


def test_a_symlink_member_is_refused(tmp_path: Path) -> None:
    """The one no rule about names will ever catch. `link -> /etc/passwd` has a perfectly
    ordinary name; a later member called `link` then writes *through* it, so two
    individually harmless members compose into a write outside the root.

    This was a CVE in Python's own `tarfile`, and the standard library's fix is a filter
    that has to be asked for by name. Deleting this test leaves the name check looking like
    the whole defence, which it says plainly it is not."""
    archive = tmp_path / "linky.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        info = zipfile.ZipInfo("link")
        # 0o120000 is the symlink bit, in the high half of the external attribute where a
        # zip carries the unix mode.
        info.external_attr = (0o120000 | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(SkillError, match="not a regular file"):
        extract_zip(archive, tmp_path / "out")


def test_a_tar_symlink_is_refused(tmp_path: Path) -> None:
    """Named rather than silently skipped: an archive containing a symlink is an archive
    somebody built to contain one, and extracting the rest of it produces a folder that
    looks like a skill and is missing whatever the link was for."""
    archive = tmp_path / "linky.tar"
    with tarfile.open(archive, "w") as tf:
        staged = tmp_path / "SKILL.md"
        staged.write_bytes(SKILL_MD)
        tf.add(staged, arcname="SKILL.md")
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    with pytest.raises(SkillError, match="not a regular file"):
        extract_tar(archive, tmp_path / "out")


def test_nothing_is_written_when_one_member_is_hostile(tmp_path: Path) -> None:
    """`safe_archive_members` refuses the archive rather than the member, and this is the
    other end of that decision. Extracting the good members and skipping the bad one
    produces a folder that passes review, because the reviewer reads what is there."""
    archive = _zip(tmp_path, {"SKILL.md": SKILL_MD, "..\\windows.txt": b"x"})
    out = tmp_path / "out"
    with pytest.raises(SkillError):
        extract_zip(archive, out)
    assert not (out / "SKILL.md").exists(), "a member was written before the archive was refused"


# --------------------------------------------------------------------- the bombs
def test_an_archive_that_declares_too_much_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """A few hundred kilobytes of zip is a terabyte of zeroes. Checked before a byte is
    written, because by the time the disk is full it is too late to check."""
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        zf.writestr("big.bin", b"\0" * (MAX_UNCOMPRESSED + 1))
    out = tmp_path / "out"
    with pytest.raises(SkillError, match="over the"):
        extract_zip(archive, out)
    assert not out.exists() or not list(out.iterdir())


def test_too_many_members_is_refused(tmp_path: Path) -> None:
    """A hundred thousand empty files never approaches the byte budget and still exhausts
    the inodes on whatever volume this lands on. The two limits catch different attacks and
    neither substitutes for the other."""
    archive = _zip(
        tmp_path,
        {"SKILL.md": SKILL_MD, **{f"f{i}.txt": b"x" for i in range(MAX_MEMBERS + 1)}},
    )
    with pytest.raises(SkillError, match="members"):
        extract_zip(archive, tmp_path / "out")


def test_an_archive_that_lies_about_its_size_is_caught_while_writing(tmp_path: Path) -> None:
    """The declared size is a claim the archive makes about itself. A running total is what
    catches an archive whose header says one thing and whose stream says another - which is
    the archive worth worrying about, since an honest one would have been refused above."""
    archive = tmp_path / "liar.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        zf.writestr("big.bin", b"\0" * 1024)
    # Rewrite the central directory's declared size to something small. The stream still
    # holds the real bytes, so only a running total catches it.
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo("big.bin")
        assert info.file_size == 1024, "the fixture did not build what this test needs"


# ------------------------------------------------------------- what makes it a skill
def test_an_archive_with_no_manifest_is_refused(tmp_path: Path) -> None:
    """No `SKILL.md` means it is not a skill. Unpacking it anyway leaves a folder somebody
    later has to work out the nature of."""
    archive = _zip(tmp_path, {"scripts/check.py": b"print(1)\n"})
    with pytest.raises(SkillError, match="not a skill"):
        extract_zip(archive, tmp_path / "out")


def test_an_archive_with_two_manifests_is_refused(tmp_path: Path) -> None:
    """Which one describes the skill would be decided by the order of the archive, which is
    a decision nobody made."""
    archive = _zip(tmp_path, {"SKILL.md": SKILL_MD, "nested/SKILL.md": SKILL_MD})
    with pytest.raises(SkillError, match="which one describes"):
        extract_zip(archive, tmp_path / "out")


# ------------------------------------------------- the second line of defence
#
# The two checks below are unreachable through a normal archive, because the name rules in
# `safe_archive_members` refuse every member that would exercise them. That makes them look
# redundant, and deleting either passes every behavioural test in this file - which is
# exactly why they are tested directly. They stop being redundant the moment the name rules
# are relaxed, and whoever relaxes them will be reading those rules, not this file.


def test_the_resolved_path_check_refuses_an_escape_the_name_rules_missed(
    tmp_path: Path,
) -> None:
    """`_assert_inside` asks the question the filesystem will actually answer: after joining
    and resolving, is the destination under the root. The name rules reason about strings
    and catch the cases worth an error message; this catches a name nobody thought of.

    Called directly, because no archive can reach it - the string rules refuse `..` and
    absolute paths first. Deleting it survives every other test here."""
    from brain.tools.extract import _assert_inside

    root = tmp_path / "out"
    root.mkdir()
    _assert_inside(root, Path("scripts/check.py"))
    with pytest.raises(SkillError, match="outside the extraction root"):
        _assert_inside(root, Path("../../etc/passwd"))
    with pytest.raises(SkillError, match="outside the extraction root"):
        _assert_inside(root, Path(tmp_path / "elsewhere.txt"))


def test_the_tar_extraction_asks_for_the_standard_library_filter_by_name() -> None:
    """`filter="data"` is the standard library's own refusal of links, devices and absolute
    paths, added in 3.12 after this class of bug turned out to be endemic.

    Asked for explicitly rather than relied on as a default, because the default changed
    between versions and a security property that depends on which Python is installed is
    not a property. Asserted on the source for the same reason the constant-time comparison
    is: the behaviour is already covered by the explicit member check above, so only the
    text says which of the two defences is present."""
    import inspect

    from brain.tools import extract

    source = inspect.getsource(extract.extract_tar)
    # The whole call, not the flag on its own. `inspect.getsource` includes the docstring,
    # and that docstring quotes `filter="data"` while explaining why it is there - so a test
    # searching for the flag alone passes with the flag removed from the code. Found by
    # mutation, and it is the second time in this repository a check has been satisfied by
    # its own explanation of itself: `sweep_tool_registry` did the same thing.
    assert 'extractall(root, filter="data")' in source, (
        "the tar extraction lost its standard-library filter"
    )


def test_the_zip_extraction_checks_each_resolved_path() -> None:
    """The call site, as opposed to the function.

    `_assert_inside` is tested directly above, but no archive can reach it through
    `extract_zip`: the name rules refuse every member that would resolve outside, so
    deleting the call passes every behavioural test here. This asserts the call exists.

    Written as a source check rather than a behaviour one because there is no behaviour to
    observe while the first line of defence holds - and it is exactly then that the second
    line gets deleted for looking redundant."""
    import inspect

    from brain.tools import extract

    source = inspect.getsource(extract.extract_zip)
    assert "_assert_inside(root, Path(info.filename))" in source, (
        "the zip extraction stopped checking where each member resolves to"
    )


def test_a_tar_that_declares_too_much_is_refused(tmp_path: Path) -> None:
    """The same budget as the zip path, checked separately because the two read their
    declared sizes from different metadata and a fix to one does not fix the other."""
    archive = tmp_path / "bigtar.tar"
    big = tmp_path / "big.bin"
    # A real file just over the budget rather than a header that lies about its size. A
    # lying header makes `addfile` read past the end of the stream, which fails with
    # "unexpected end of data" - a different failure, and not the one being tested.
    big.write_bytes(b"\0" * (MAX_UNCOMPRESSED + 1))
    with tarfile.open(archive, "w") as tf:
        staged = tmp_path / "SKILL.md"
        staged.write_bytes(SKILL_MD)
        tf.add(staged, arcname="SKILL.md")
        tf.add(big, arcname="big.bin")
    with pytest.raises(SkillError, match="over the"):
        extract_tar(archive, tmp_path / "out")
