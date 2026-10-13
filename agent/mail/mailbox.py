"""
Mailbox abstraction and FolderMailbox implementation using Python's standard library email package.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import email
from email import policy
import glob
import hashlib
import os
import re
from typing import Any, Dict, List, Optional


@dataclass
class AttachmentInfo:
    filename: str
    content_type: str
    size: int
    sha256: str
    data: bytes = field(repr=False)


@dataclass
class MailMessage:
    message_id: str
    subject: str
    from_address: str
    to_address: str
    date: str
    body_text: str
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    reply_to: Optional[str] = None
    attachments: List[AttachmentInfo] = field(default_factory=list)
    raw_path: Optional[str] = None


class Mailbox(ABC):
    @abstractmethod
    def list_emails(self) -> List[MailMessage]:
        pass

    @abstractmethod
    def get_email(self, message_id: str) -> Optional[MailMessage]:
        pass

    @abstractmethod
    def save_sent_email(self, msg_bytes: bytes, filename: str) -> str:
        pass


class FolderMailbox(Mailbox):
    def __init__(self, inbox_dir: Optional[str] = None, sent_dir: Optional[str] = None) -> None:
        from agent.config import get_inbox_mailbox_dir, get_sent_mailbox_dir
        self.inbox_dir = inbox_dir or get_inbox_mailbox_dir()
        self.sent_dir = sent_dir or get_sent_mailbox_dir()
        os.makedirs(self.inbox_dir, exist_ok=True)
        os.makedirs(self.sent_dir, exist_ok=True)

    @staticmethod
    def parse_eml_bytes(data: bytes, file_path: Optional[str] = None) -> MailMessage:
        msg = email.message_from_bytes(data, policy=policy.default)
        msg_id = msg.get("Message-ID", "")
        if not msg_id and file_path:
            msg_id = f"<{os.path.basename(file_path)}>"
        # Clean message_id brackets for clean handling if present
        msg_id_clean = msg_id.strip("<>").strip()

        subject = msg.get("Subject", "")
        from_addr = msg.get("From", "")
        to_addr = msg.get("To", "")
        date_str = msg.get("Date", "")
        in_reply_to = msg.get("In-Reply-To", None)
        if in_reply_to:
            in_reply_to = in_reply_to.strip("<>").strip()
        references = msg.get("References", None)
        reply_to = msg.get("Reply-To", None)

        body_parts: List[str] = []
        attachments: List[AttachmentInfo] = []

        if msg.is_multipart():
            for part in msg.walk():
                content_disposition = str(part.get("Content-Disposition", ""))
                content_type = part.get_content_type()
                filename = part.get_filename()

                if filename or "attachment" in content_disposition.lower():
                    att_data = part.get_payload(decode=True) or b""
                    att_name = filename or "unnamed_attachment"
                    att_hash = hashlib.sha256(att_data).hexdigest()
                    attachments.append(AttachmentInfo(
                        filename=att_name,
                        content_type=content_type,
                        size=len(att_data),
                        sha256=att_hash,
                        data=att_data,
                    ))
                elif content_type == "text/plain":
                    text = part.get_content()
                    if isinstance(text, str):
                        body_parts.append(text)
                    elif isinstance(text, bytes):
                        body_parts.append(text.decode("utf-8", errors="replace"))
        else:
            text = msg.get_content()
            if isinstance(text, str):
                body_parts.append(text)
            elif isinstance(text, bytes):
                body_parts.append(text.decode("utf-8", errors="replace"))

        body_full = "\n\n".join(body_parts).strip()

        return MailMessage(
            message_id=msg_id_clean,
            subject=subject,
            from_address=from_addr,
            to_address=to_addr,
            date=date_str,
            body_text=body_full,
            in_reply_to=in_reply_to,
            references=references,
            reply_to=reply_to,
            attachments=attachments,
            raw_path=file_path,
        )

    def list_emails(self) -> List[MailMessage]:
        eml_files = sorted(glob.glob(os.path.join(self.inbox_dir, "*.eml")))
        messages = []
        for path in eml_files:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                msg = self.parse_eml_bytes(data, file_path=path)
                messages.append(msg)
            except Exception:
                continue
        return messages

    def get_email(self, message_id: str) -> Optional[MailMessage]:
        target = message_id.strip("<>").strip()
        for msg in self.list_emails():
            if msg.message_id == target or msg.message_id == f"<{target}>" or target in msg.message_id:
                return msg
        return None

    def save_sent_email(self, msg_bytes: bytes, filename: str) -> str:
        # Sanitize filename
        safe_name = os.path.basename(filename)
        out_path = os.path.join(self.sent_dir, safe_name)
        with open(out_path, "wb") as f:
            f.write(msg_bytes)
        return out_path

