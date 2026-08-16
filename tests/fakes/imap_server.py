from __future__ import annotations

from .fixtures import RFC2047_MESSAGE, pdf_message


class FakeImap:
    def __init__(self, host: str = "imap.example.com", port: int = 993, **kwargs) -> None:
        self.host = host
        self.port = port
        self.commands: list[str] = []
        self.uidvalidity = "777"
        self.auth_fail = False
        self.message = pdf_message()
        self.encoded_message = RFC2047_MESSAGE

    def factory(self):
        """Use this fake wherever an ``imaplib.IMAP4_SSL`` constructor is expected."""
        return lambda *args, **kwargs: self

    def login(self, username: str, password: str):
        self.commands.append(f"LOGIN {username}")
        if self.auth_fail:
            return "NO", [b"AUTHENTICATIONFAILED"]
        return "OK", [b"logged in"]

    def examine(self, mailbox: str):
        self.commands.append(f"EXAMINE {mailbox}")
        return "OK", [f"[UIDVALIDITY {self.uidvalidity}]".encode(), b"2"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        self.commands.append(("EXAMINE" if readonly else "SELECT") + f" {mailbox}")
        return "OK", [f"[UIDVALIDITY {self.uidvalidity}]".encode()]

    def response(self, code: str):
        if code == "UIDVALIDITY":
            return "OK", [self.uidvalidity.encode()]
        return "NO", [None]

    def list(self):
        self.commands.append("LIST")
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Travel &ZeVnLIqe-"', b'(\\HasNoChildren) "/" "Receipts 2026"']

    def uid(self, command: str, *args):
        self.commands.append("UID " + command + " " + " ".join(str(a) for a in args if a is not None))
        if command == "SEARCH":
            return "OK", [b"101 102"]
        if command == "FETCH":
            uid = str(args[0])
            spec = str(args[1])
            msg = self.encoded_message if uid == "102" else self.message
            if "BODY.PEEK[]" in spec:
                return "OK", [msg]
            headers = msg.split(b"\n\n", 1)[0] + b"\n\n"
            bodystructure = b'BODYSTRUCTURE (("TEXT" "PLAIN")("APPLICATION" "PDF" NIL NIL NIL "BASE64" 12 NIL ("ATTACHMENT" ("FILENAME" "receipt.pdf"))))'
            return "OK", [headers + bodystructure]
        return "BAD", [b"unknown"]
