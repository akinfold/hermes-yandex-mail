"""Protocol grammar and decoding boundaries used by message pages."""

import base64
import quopri

import pytest

from hermes_yandex_mail.mime import (
    MessageNotFound,
    StructureParser,
    flatten_parts,
    response_fields,
)
from hermes_yandex_mail.paging import body_text, decoded_chunks, take_page, text_chunks


@pytest.mark.parametrize("encoding", ["base64", "quoted-printable", "7bit", "8bit", "binary"])
@pytest.mark.parametrize("width", [1, 2, 7, 65536])
def test_transfer_decoding_survives_every_network_boundary(encoding, width):
    raw = "A🙂Б=\r\nlast line\r\n".encode()
    encoded = raw
    if encoding == "base64":
        encoded = base64.encodebytes(raw)
    if encoding == "quoted-printable":
        encoded = quopri.encodestring(raw)
    chunks = [encoded[i : i + width] for i in range(0, len(encoded), width)]
    assert b"".join(decoded_chunks(chunks, encoding)) == raw


@pytest.mark.parametrize("charset", ["utf-8", "cp1251", "unknown-charset"])
def test_text_characters_and_crlf_survive_network_boundaries(charset):
    raw = "Hello, мир\r\nlast\r".encode(charset if charset != "unknown-charset" else "utf-8")
    assert (
        "".join(text_chunks([raw[i : i + 1] for i in range(len(raw))], charset))
        == "Hello, мир\nlast\r"
    )


def test_html_page_offsets_apply_after_tags_entities_and_hidden_text():
    raw = b"<head><title>hidden</title></head><p>A &amp; B</p><script>hidden</script><br>C"

    def chunks():
        text = text_chunks([raw[i : i + 1] for i in range(len(raw))], "utf-8")
        return body_text([text], html=True)

    expected = "A & B\n\nC"
    assert "".join(chunks()) == expected
    assert take_page(chunks(), 2, 3, "") == ("& B", False)
    assert take_page(chunks(), 5, 20, "") == ("\n\nC", True)


@pytest.mark.parametrize(
    "payload,encoding,expected",
    [
        (b"SGVsbG8gd29ybGQ", "base64", b"Hello world"),  # final padding missing
        (b"SGVsbG8=\r\nIHdvcmxk\r\n", "base64", b"Hello world"),  # two blocks, one after another
        (b"SGVs@bG8*gd29y!bGQ=", "base64", b"Hello world"),  # stray characters
        (b"YW", "base64", b"a"),
        (b"@@@=", "base64", b""),
        (b"QUJDR", "base64", b"ABC"),  # a lone trailing character carries no byte
        (b"plain", "x-unknown", b"plain"),
        (b"plain", "rot13", b"plain"),
    ],
)
def test_sloppy_encodings_decode_as_leniently_as_the_email_package(payload, encoding, expected):
    for width in (1, 3, len(payload)):
        chunks = [payload[i : i + width] for i in range(0, len(payload), width)]
        assert b"".join(decoded_chunks(chunks, encoding)) == expected


def test_quoted_printable_flushes_a_trailing_literal():
    assert b"".join(decoded_chunks([b"x=Z"], "quoted-printable")) == b"x=Z"


@pytest.mark.parametrize(
    "offset,limit,expected",
    [(0, 4, ("abcd", True)), (4, 1, ("", True)), (100, 3, ("", True)), (0, 3, ("abc", False))],
)
def test_exact_end_and_out_of_range_pages(offset, limit, expected):
    assert take_page(iter(["a", "", "bcd"]), offset, limit, "") == expected


def test_structure_literals_escaped_names_and_uid_after_body():
    data = [
        (b'1 (BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" {7}', b'a"b.txt'),
        b') NIL NIL "7BIT" 12 1) UID 8)',
    ]
    structure = response_fields(data, "8")["BODYSTRUCTURE"]
    part = flatten_parts(structure)[0]
    assert part.filename == 'a"b.txt'
    assert part.part_id == "1"
    assert part.attachment is True
    assert StructureParser(b'"a\\"b\\\\c"').value() == 'a"b\\c'


def test_binary_literal_bytes_are_preserved_and_updates_merged():
    raw = b"\x00\xff()\r\n"
    data = [b"1 (UID 8 FLAGS (\\Seen))", (b"1 (BODY[2]<0> {6}", raw), b" UID 8)"]
    fields = response_fields(data, "8", literal_bytes=True)
    assert fields["BODY[2]<0>"] == raw
    assert fields["FLAGS"] == ["\\Seen"]


def test_nested_parts_keep_numbers_and_rfc2231_filename():
    structure = StructureParser(
        b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 5 1) '
        b'("TEXT" "HTML" NIL NIL NIL "7BIT" 10 1) "ALTERNATIVE")'
    ).value()
    outer = [
        structure,
        [
            "APPLICATION",
            "PDF",
            None,
            None,
            None,
            "BASE64",
            "42",
            None,
            ["ATTACHMENT", ["FILENAME*", "utf-8''%D0%A2%D0%B5%D1%81%D1%82.pdf"]],
        ],
        "MIXED",
    ]
    parts = flatten_parts(outer)
    assert [p.part_id for p in parts] == ["1.1", "1.2", "2"]
    assert parts[2].filename == "Тест.pdf"
    assert parts[2].attachment_info()["size"] == 30


def test_attached_message_and_multipart_children_are_not_body_text():
    message = ["MESSAGE", "RFC822", None, None, None, "7BIT", "99", [], [], "2", None, None]
    text = ["TEXT", "PLAIN", None, None, None, "7BIT", "12", "1"]
    assert flatten_parts(message)[0].attachment
    assert flatten_parts([text, "MIXED", None, ["ATTACHMENT", None]])[0].attachment
    assert flatten_parts(["TEXT", "CALENDAR", None, None, None, "7BIT", "12", "1"])[0].attachment


@pytest.mark.parametrize(
    "raw", [b"(", b'"unterminated', b"{4}\r\nx", b"{bad}", b")", b"(" * 42 + b")" * 42]
)
def test_malformed_structure_is_rejected(raw):
    with pytest.raises(ValueError):
        StructureParser(raw).value()


def test_structure_limits_and_missing_uid():
    with pytest.raises(ValueError, match="metadata limit"):
        StructureParser(b"x" * (1024 * 1024 + 1))
    with pytest.raises(MessageNotFound, match="not returned"):
        response_fields([None, b"1 (UID 9 FLAGS ())"], "8")
    with pytest.raises(ValueError, match="size"):
        flatten_parts(["TEXT", "PLAIN", None, None, None, "7BIT", "-1", "1"])


def test_unterminated_charset_sequences_cannot_grow_without_bound():
    with pytest.raises(ValueError, match="decoder"):
        list(text_chunks([b"+" + b"A" * 65535, b"A" * 65536], "utf-7"))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("utf-8''%D0%A2%D0%B5%D1%81%D1%82.pdf", "Тест.pdf"),
        ("windows-1251''%D2%E5%F1%F2.pdf", "Тест.pdf"),
        ("utf-8'ru'%D0%9E%D1%82.pdf", "От.pdf"),
        ("nosuchcharset''a%20b.pdf", "nosuchcharset''a%20b.pdf"),
        ("utf-8''%FF.pdf", "utf-8''%FF.pdf"),
        ("it's 100%.pdf", "it's 100%.pdf"),
        ("=?utf-8?B?0KLQtdGB0YI=?=.pdf", "Тест.pdf"),
    ],
)
def test_filenames_under_the_plain_key_are_decoded_when_they_are_encoded(value, expected):
    node = ["APPLICATION", "PDF", None, None, None, "BASE64", "4", None]
    node.append(["ATTACHMENT", ["FILENAME", value]])
    assert flatten_parts(node)[0].filename == expected
