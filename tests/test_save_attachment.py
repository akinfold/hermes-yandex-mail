"""Saving an attachment to a file: the name, the write, the cap, the tool."""

import base64
import hashlib
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

from hermes_yandex_mail import _compat, attachment, config, tool
from hermes_yandex_mail.attachment import AttachmentTooLarge, prune, safe_filename, save
from hermes_yandex_mail.imap import YandexIMAPClient

from .test_paging import YANDEX_CYRILLIC_ATTACHMENT, YANDEX_FORWARDED, MimeIMAP

# -- the name ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Отчёт за сентябрь.pdf", "Отчёт за сентябрь.pdf"),
        ("archive.tar.gz", "archive.tar.gz"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\Windows\\evil.exe", "evil.exe"),
        ("a\x00b\x07c\r\n.txt", "abc.txt"),
        ("invoice\u202egpj.exe", "invoicegpj.exe"),
        (".bashrc", "bashrc"),
        ("  report . ", "report"),
        ('a<b>c:"d|e?f*.txt', "a_b_c__d_e_f_.txt"),
        ("", "attachment-2.bin"),
        ("...", "attachment-2.bin"),
        ("/", "attachment-2.bin"),
        ("\u202e\x00", "attachment-2.bin"),
    ],
)
def test_a_name_becomes_one_harmless_path_component(name, expected):
    assert safe_filename(name, "2") == expected


def test_a_long_name_is_cut_on_a_character_boundary_and_keeps_its_extension():
    name = safe_filename("я" * 300 + ".pdf", "2")
    assert name.endswith(".pdf")
    assert len(name.encode()) <= 180
    assert set(name.removesuffix(".pdf")) == {"я"}


def test_an_extension_too_long_to_be_one_is_part_of_the_name():
    name = safe_filename("file." + "x" * 40, "2")
    assert name == "file." + "x" * 40


# -- the write ---------------------------------------------------------------


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def test_save_writes_the_bytes_privately_and_reports_their_hash(tmp_path):
    directory = tmp_path / "cache" / "documents"
    data = bytes(range(256)) * 100
    saved = save([data[:1000], data[1000:]], directory, "Отчёт.pdf", 10**6)
    assert saved.path.parent == directory
    assert saved.path.name.startswith("yandex-mail_")
    assert saved.path.name.endswith("_Отчёт.pdf")
    assert saved.path.read_bytes() == data
    assert saved.size == len(data)
    assert saved.sha256 == hashlib.sha256(data).hexdigest()
    assert stat.S_IMODE(saved.path.stat().st_mode) == 0o600
    assert _files(directory) == [saved.path.name]


def test_an_attachment_exactly_at_the_cap_is_saved(tmp_path):
    assert save([b"x" * 10], tmp_path, "a.bin", 10).size == 10


def test_an_attachment_over_the_cap_leaves_nothing_behind(tmp_path):
    with pytest.raises(AttachmentTooLarge, match="10-byte limit"):
        save([b"x" * 6, b"x" * 6], tmp_path, "a.bin", 10)
    assert _files(tmp_path) == []


def test_a_failure_midway_leaves_nothing_behind(tmp_path):
    def chunks():
        yield b"first"
        raise OSError("connection reset")

    with pytest.raises(OSError, match="connection reset"):
        save(chunks(), tmp_path, "a.bin", 100)
    assert _files(tmp_path) == []


def test_an_existing_file_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(attachment.uuid, "uuid4", lambda: types.SimpleNamespace(hex="0" * 32))
    existing = tmp_path / "yandex-mail_000000000000_a.bin"
    existing.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        save([b"new"], tmp_path, "a.bin", 100)
    assert existing.read_bytes() == b"precious"
    assert _files(tmp_path) == [existing.name]


def test_a_planted_link_at_the_temporary_name_is_not_written_through(tmp_path, monkeypatch):
    monkeypatch.setattr(attachment.uuid, "uuid4", lambda: types.SimpleNamespace(hex="0" * 32))
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    (tmp_path / ".yandex-mail_000000000000.part").symlink_to(victim)
    with pytest.raises(FileExistsError):
        save([b"new"], tmp_path, "a.bin", 100)
    assert victim.read_bytes() == b"precious"


def test_without_hard_links_the_file_is_renamed_into_place(tmp_path, monkeypatch):
    def no_links(source, target):
        raise PermissionError("hard links are not supported here")

    monkeypatch.setattr(attachment.os, "link", no_links)
    saved = save([b"data"], tmp_path, "a.bin", 100)
    assert saved.path.read_bytes() == b"data"
    assert _files(tmp_path) == [saved.path.name]


def test_without_hard_links_an_existing_file_is_still_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(attachment.os, "link", lambda s, t: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(attachment.uuid, "uuid4", lambda: types.SimpleNamespace(hex="0" * 32))
    existing = tmp_path / "yandex-mail_000000000000_a.bin"
    existing.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        save([b"new"], tmp_path, "a.bin", 100)
    assert existing.read_bytes() == b"precious"


def test_a_short_write_is_continued(tmp_path, monkeypatch):
    real_write = os.write
    monkeypatch.setattr(attachment.os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))
    saved = save([b"0123456789"], tmp_path, "a.bin", 100)
    assert saved.path.read_bytes() == b"0123456789"


# -- keeping the cache small ---------------------------------------------------


def test_prune_removes_only_this_plugins_old_files(tmp_path):
    now = 1_000_000.0
    old, new = now - 25 * 3600, now - 3600
    files = {
        "yandex-mail_aaa_old.pdf": old,
        ".yandex-mail_bbb.part": old,
        "yandex-mail_ccc_new.pdf": new,
        "doc_ddd_someone_elses.pdf": old,
    }
    for name, mtime in files.items():
        path = tmp_path / name
        path.write_bytes(b"x")
        os.utime(path, (mtime, mtime))
    nested = tmp_path / "yandex-mail_dir"
    nested.mkdir()
    os.utime(nested, (old, old))
    link = tmp_path / "yandex-mail_link"
    link.symlink_to(tmp_path / "doc_ddd_someone_elses.pdf")
    os.utime(link, (old, old), follow_symlinks=False)
    assert prune(tmp_path, 24 * 3600, now=now) == 2
    assert _files(tmp_path) == [
        "doc_ddd_someone_elses.pdf",
        "yandex-mail_ccc_new.pdf",
        "yandex-mail_dir",
        "yandex-mail_link",
    ]


def test_prune_of_a_directory_that_does_not_exist_yet_is_harmless(tmp_path):
    assert prune(tmp_path / "missing", 60) == 0


# -- where the file goes, inside and outside Hermes ------------------------------


def test_outside_hermes_the_cache_is_under_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert _compat.document_cache_dir() == tmp_path / "home" / "cache" / "documents"
    monkeypatch.delenv("HERMES_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _compat.document_cache_dir() == tmp_path / ".hermes" / "cache" / "documents"
    assert _compat.agent_visible_path(tmp_path / "x.pdf") == str(tmp_path / "x.pdf")


def test_inside_hermes_its_own_resolution_is_used(monkeypatch):
    calls = []

    def get_hermes_dir(new_subpath, old_name):
        calls.append((new_subpath, old_name))
        return Path("/profile/cache/documents")

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_dir = get_hermes_dir
    credential_files = types.ModuleType("tools.credential_files")
    credential_files.to_agent_visible_cache_path = lambda p: "/root/.hermes/" + Path(p).name
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.credential_files", credential_files)
    assert _compat.document_cache_dir() == Path("/profile/cache/documents")
    assert calls == [("cache/documents", "document_cache")]
    assert _compat.agent_visible_path(Path("/host/x.pdf")) == "/root/.hermes/x.pdf"


# -- the size cap setting ------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    values: dict[str, str] = {}
    monkeypatch.setattr(config, "get_provider_env", lambda name: values.get(name, "").strip())
    monkeypatch.setattr(config, "provider_env_is_set", lambda name: name in values)
    return values


def test_the_cap_defaults_to_100_mib(env):
    assert config.attachment_max_bytes() == 100 * 1024 * 1024


def test_the_cap_can_be_set_in_bytes(env):
    env[config.ENV_ATTACHMENT_MAX_BYTES] = "1048576"
    assert config.attachment_max_bytes() == 1048576


@pytest.mark.parametrize("value", ["100MB", "10 MiB", "0", "-5", "  ", "1.5"])
def test_a_cap_that_is_not_a_byte_count_refuses_every_save(env, value):
    env[config.ENV_ATTACHMENT_MAX_BYTES] = value
    with pytest.raises(config.PermissionDenied, match="positive whole number of bytes"):
        config.attachment_max_bytes()


# -- the tool ----------------------------------------------------------------

REPORT = bytes(range(256)) * 1172  # 300 032 bytes, the planted report's size


@pytest.fixture
def mailbox(monkeypatch):
    fake = MimeIMAP()
    fake.response = YANDEX_CYRILLIC_ATTACHMENT
    fake.parts = {"1": b"See attached.\r\n", "2": base64.encodebytes(REPORT)}
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient("fixture", "fixture", connection_factory=lambda *_: fake),
    )
    monkeypatch.delenv(config.ENV_ATTACHMENT_MAX_BYTES, raising=False)
    return fake


def _save(**args):
    return json.loads(tool.handle_save_attachment({"uid": "8", "folder": "INBOX", **args}))


def _cache() -> Path:
    return Path(os.environ["HERMES_HOME"]) / "cache" / "documents"


def test_the_attachment_is_saved_and_only_its_location_comes_back(mailbox):
    result = _save(part_id="2")["attachment"]
    path = Path(result["path"])
    assert path.parent == _cache()
    assert path.name.endswith("_Отчёт за сентябрь.pdf")
    assert path.read_bytes() == REPORT
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert result == {
        "uid": "8",
        "folder": "INBOX",
        "part_id": "2",
        "filename": "Отчёт за сентябрь.pdf",
        "content_type": "application/pdf",
        "size": len(REPORT),
        "sha256": hashlib.sha256(REPORT).hexdigest(),
        "path": str(path),
    }


def test_saving_asks_for_large_chunks_and_never_marks_the_message_read(mailbox):
    _save(part_id="2")
    fetches = [c[3] for c in mailbox.calls if c[0] == "uid" and "BODY.PEEK[2]" in c[3]]
    assert fetches[0] == "(UID BODY.PEEK[2]<0.524288>)"
    assert not any(c[0] == "uid" and "BODY[" in c[3] for c in mailbox.calls if len(c) > 3)
    assert not any(c[0] == "uid" and c[1] == "STORE" for c in mailbox.calls)


def test_the_path_is_the_one_the_agent_sees(mailbox, monkeypatch):
    monkeypatch.setattr(tool, "agent_visible_path", lambda p: "/root/.hermes/cache/documents/x")
    assert _save(part_id="2")["attachment"]["path"] == "/root/.hermes/cache/documents/x"


@pytest.mark.parametrize("part_id", ["1", "9", "2.1", "2\r\nX NOOP", "../2"])
def test_only_a_listed_attachment_can_be_saved(mailbox, part_id):
    result = _save(part_id=part_id)
    assert "has no attachment with part_id" in result["error"]
    assert not any("BODY.PEEK[" in str(c) for c in mailbox.calls)
    assert _files(_cache()) == []


def test_part_id_is_required(mailbox):
    assert "'part_id' is required" in _save()["error"]
    assert not mailbox.calls


def test_an_attachment_over_the_cap_is_refused_and_not_kept(mailbox, monkeypatch):
    monkeypatch.setenv(config.ENV_ATTACHMENT_MAX_BYTES, "100000")
    assert "100000-byte limit" in _save(part_id="2")["error"]
    assert _files(_cache()) == []


def test_a_mistyped_cap_refuses_before_touching_the_mailbox(mailbox, monkeypatch):
    monkeypatch.setenv(config.ENV_ATTACHMENT_MAX_BYTES, "100MB")
    assert "positive whole number" in _save(part_id="2")["error"]
    assert not mailbox.calls


def test_a_forwarded_message_is_saved_as_an_eml_file(mailbox):
    mailbox.response = YANDEX_FORWARDED
    mailbox.parts = {"2": b"Subject: inner\r\n\r\nForwarded text survives.\r\n"}
    result = _save(part_id="2")["attachment"]
    assert result["filename"] == "(unnamed)"
    assert result["content_type"] == "message/rfc822"
    assert Path(result["path"]).name.endswith("_message-2.eml")
    assert Path(result["path"]).read_bytes() == mailbox.parts["2"]


def test_each_save_prunes_this_plugins_old_files(mailbox):
    _cache().mkdir(parents=True)
    stale, foreign = _cache() / "yandex-mail_000000000000_old.pdf", _cache() / "doc_x_old.pdf"
    for path in (stale, foreign):
        path.write_bytes(b"x")
        os.utime(path, (0, 0))
    _save(part_id="2")
    assert not stale.exists()
    assert foreign.exists()


def test_the_folder_allow_list_holds(monkeypatch):
    fake = MimeIMAP()
    monkeypatch.setattr(
        tool,
        "build_client",
        lambda: YandexIMAPClient(
            "fixture", "fixture", allowed_folders=["INBOX"], connection_factory=lambda *_: fake
        ),
    )
    result = json.loads(tool.handle_save_attachment({"uid": "8", "folder": "Sent", "part_id": "2"}))
    assert "not in the allowed list" in result["error"]
    assert not any(call[0] in {"select", "uid"} for call in fake.calls)


def test_a_read_only_deployment_cannot_save(mailbox, monkeypatch):
    monkeypatch.setattr(config, "get_provider_env", lambda name: "read")
    guarded = tool.with_action_guard("save_attachment", tool.handle_save_attachment)
    result = json.loads(guarded({"uid": "8", "folder": "INBOX", "part_id": "2"}))
    assert "not allowed" in result["error"]
    assert not mailbox.calls
    assert _files(_cache()) == []
