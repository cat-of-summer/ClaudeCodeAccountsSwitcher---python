from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from core import store
from ui import i18n


class TempHome(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)

        self._original_home = Path.home
        Path.home = classmethod(lambda cls, _h=self.home: _h)

        self._original_env = dict(os.environ)
        os.environ["CCAS_HOME"] = str(self.home / ".claude-switcher")
        os.environ["CCAS_LANG"] = "en"
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("SHELL", None)

        i18n.set_language("en")
        store.ensure_layout()

    def tearDown(self) -> None:
        Path.home = self._original_home
        os.environ.clear()
        os.environ.update(self._original_env)
        i18n.set_language(None)
        self._tmp.cleanup()

    def write_root_config(self, email: str, user_id: str = "uid") -> Path:
        path = self.home / ".claude.json"
        store.write_json_atomic(
            path,
            {
                "numStartups": 7,
                "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{email}"},
                "userID": user_id,
            },
            harden=False,
        )
        return path

    def identity(self, email: str, user_id: str = "uid") -> dict:
        return {
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{email}"},
            "userID": user_id,
        }

    def write_credentials(self, path: Path, **oauth: object) -> Path:
        payload = {
            "accessToken": "tok",
            "refreshToken": "ref",
            "expiresAt": 99999999999999,
            "refreshTokenExpiresAt": 99999999999999,
        }
        payload.update(oauth)
        store.write_json_atomic(path, {"claudeAiOauth": payload}, harden=False)
        return path

    def fake_claude(self, body: str, name: str = "fake_claude") -> Path:
        """A stand-in for the claude binary, executable on this platform.

        The transport puts its own flags first, so the fake cannot ride in as
        a default argument -- it has to *be* `real_claude_path`. Windows will
        not exec a .py by its shebang, hence the .cmd in front of it there.
        """
        source = self.home / f"{name}.py"
        source.write_text(body, encoding="utf-8")
        if os.name == "nt":
            shim = self.home / f"{name}.cmd"
            shim.write_text(f'@"{sys.executable}" "{source}" %*\r\n', encoding="utf-8")
            return shim
        source.chmod(0o755)
        return source
