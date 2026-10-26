from __future__ import annotations

import json
from email.message import EmailMessage

from app.core.config import get_settings
from app.services.email_parse import parse_email
from app.services.render import render_email


def multipart_mail() -> bytes:
    mail = EmailMessage()
    mail["From"] = "홍길동 <hong@example.com>"
    mail["To"] = "me@naver.com"
    mail["Subject"] = "견적서 보내드립니다"
    mail["Date"] = "Tue, 20 Oct 2026 09:35:53 +0900"
    mail["Message-ID"] = "<q-1@example.com>"
    mail.set_content("<!-- Header -->\n안녕하세요.\n\n\n\n견적서 확인 부탁드립니다.\n"
                     "https://example.com/doc/1?utm_source=mail&utm_campaign=x\n", cte="base64")
    mail.add_alternative("<html><body><p>안녕하세요.</p></body></html>", subtype="html")
    mail.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="견적서.pdf")
    return bytes(mail)


def test_parse_decodes_body_and_lists_attachments(tmp_path):
    path = tmp_path / "a.eml"
    path.write_bytes(multipart_mail())
    parsed = parse_email(path)
    assert parsed["subject"] == "견적서 보내드립니다"
    assert parsed["text"] == ("안녕하세요.\n\n견적서 확인 부탁드립니다.\n"
                              "https://example.com/doc/1?utm_source=mail&utm_campaign=x")
    assert parsed["attachments"] == [{"filename": "견적서.pdf", "content_type": "application/pdf", "size": 13}]
    assert parsed["date_text"] == "2026-10-20 09:35:53"   # 한국 시간


def test_html_only_mail_is_converted_to_text(tmp_path):
    path = tmp_path / "b.eml"
    path.write_bytes(
        b"From: a@example.com\r\nSubject: hi\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><style>p{color:red}</style><p>first&nbsp;line</p><div>second<br>third</div></html>")
    assert parse_email(path)["text"] == "first line\nsecond\nthird"


def test_ingest_writes_parsed_json_and_render_strips_tracking_query(client):
    event_id = client.post("/v1/ingest/emails?account=naver", content=multipart_mail()).json()["event_id"]
    emails = get_settings().raw_dir / "emails"
    (eml,) = emails.rglob("*.eml")
    assert eml.parent.name == "20261020"   # Date 헤더(한국 시간) 기준 날짜 폴더
    stored = json.loads(eml.with_suffix(".json").read_text())
    assert stored["attachments"][0]["filename"] == "견적서.pdf"

    text = render_email({"account": "naver"}, eml)
    assert "https://example.com/doc/1\n" in text + "\n"
    assert "utm_" not in text
    assert "첨부: application/pdf 견적서.pdf" in text
    assert event_id == 1


def test_render_parses_mail_received_before_json_existed(client):
    client.post("/v1/ingest/emails?account=naver", content=multipart_mail())
    (eml,) = (get_settings().raw_dir / "emails").rglob("*.eml")
    eml.with_suffix(".json").unlink()
    assert "견적서 확인 부탁드립니다." in render_email({"account": "naver"}, eml)
    assert eml.with_suffix(".json").exists()


def test_save_attachments_sanitizes_names_and_is_idempotent(tmp_path):
    from app.services.email_parse import attachment_dir, save_attachments, saved_attachments
    mail = EmailMessage()
    mail["Subject"] = "x"
    mail.set_content("본문")
    mail.add_attachment(b"a", maintype="text", subtype="plain", filename="../../evil.txt")
    mail.add_attachment(b"b", maintype="text", subtype="plain", filename="evil.txt")
    mail.add_attachment(b"c", maintype="application", subtype="octet-stream")
    eml = tmp_path / "20261020" / "abc.eml"
    eml.parent.mkdir()
    eml.write_bytes(bytes(mail))
    assert saved_attachments(eml) == []
    saved = save_attachments(eml)
    out = attachment_dir(eml)
    assert out == get_settings().attachments_dir / "emails" / "20261020" / "abc"
    assert sorted(p.name for p in saved) == ["2-evil.txt", "attachment-3", "evil.txt"]
    assert all(p.parent == out for p in saved)
    assert save_attachments(eml) == saved == saved_attachments(eml)
