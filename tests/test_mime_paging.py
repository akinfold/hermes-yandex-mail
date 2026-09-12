"""Protocol grammar and decoding boundaries used by message and attachment pages."""

import base64
import quopri

import pytest

from hermes_yandex_mail.mime import StructureParser, flatten_parts, response_fields
from hermes_yandex_mail.paging import decoded_chunks, take_page, text_chunks


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
        return text_chunks([raw[i : i + 1] for i in range(len(raw))], "utf-8", html=True)

    expected = "A & B\n\nC"
    assert "".join(chunks()) == expected
    assert take_page(chunks(), 2, 3, "") == ("& B", False)
    assert take_page(chunks(), 5, 20, "") == ("\n\nC", True)


@pytest.mark.parametrize(
    "payload,encoding", [(b"YW", "base64"), (b"@@@=", "base64"), (b"x", "rot13")]
)
def test_invalid_encodings_fail_explicitly(payload, encoding):
    with pytest.raises(ValueError):
        list(decoded_chunks([payload], encoding))


def test_base64_rejects_data_after_padding():
    with pytest.raises(ValueError):
        list(decoded_chunks([b"eA==", b"eA=="], "base64"))


def test_quoted_printable_flushes_a_trailing_literal():
    assert b"".join(decoded_chunks([b"x=Z"], "quoted-printable")) == b"x=Z"


def test_html_parser_limits_incomplete_tokens():
    with pytest.raises(ValueError, match="HTML token"):
        list(text_chunks([b"<!--" + b"x" * 65536], "utf-8", html=True))


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
    assert parts[2].attachment_info()["encoded_size"] == 42
    assert parts[2].attachment_info()["size"] is None


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
    with pytest.raises(ValueError, match="not returned"):
        response_fields([None, b"1 (UID 9 FLAGS ())"], "8")
    with pytest.raises(ValueError, match="size"):
        flatten_parts(["TEXT", "PLAIN", None, None, None, "7BIT", "-1", "1"])


def test_unterminated_charset_sequences_cannot_grow_without_bound():
    with pytest.raises(ValueError, match="decoder"):
        list(text_chunks([b"+" + b"A" * 65535, b"A" * 65536], "utf-7"))
