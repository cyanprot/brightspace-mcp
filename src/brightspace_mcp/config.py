import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Brightspace MCP server configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.brightspace_url: str = os.environ.get(
            "BRIGHTSPACE_URL", "https://d2l.langara.bc.ca"
        ).rstrip("/")
        self.username: str = os.environ.get("BRIGHTSPACE_USER", "")
        self.session_dir: Path = Path(
            os.environ.get(
                "BRIGHTSPACE_SESSION_DIR", Path.home() / ".local/state/brightspace-mcp"
            )
        )
        self.headless: bool = os.environ.get("BRIGHTSPACE_HEADLESS", "false").lower() == "true"
        self.lp_version: str = "1.57"
        self.le_version: str = "1.92"
        self.download_dir: Path = Path(
            os.environ.get("BRIGHTSPACE_DOWNLOAD_DIR", self.session_dir / "downloads")
        )

        # 0700: the directory holds the browser profile and the session cookies.
        self.session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def storage_state_path(self) -> Path:
        return self.session_dir / "storage_state.json"

    @property
    def profile_dir(self) -> Path:
        """Persistent Chromium profile holding the Microsoft SSO session."""
        return self.session_dir / "chrome-profile"

    @property
    def cookie_age_seconds(self) -> float | None:
        """Seconds since cookies were last saved. None if no saved state."""
        if not self.storage_state_path.exists():
            return None
        return time.time() - self.storage_state_path.stat().st_mtime

    # Microsoft AAD sign-in. Only used to prefill the email during the manual
    # bootstrap and to tell "still on the AAD page" from "landed on Brightspace".
    MS_LOGIN_HOST = "login.microsoftonline.com"
    MS_EMAIL_INPUT = 'input[type="email"][name="loginfmt"]'
