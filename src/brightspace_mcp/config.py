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
        self.password: str = os.environ.get("BRIGHTSPACE_PASS", "")
        self.session_dir: Path = Path(
            os.environ.get("BRIGHTSPACE_SESSION_DIR", Path.home() / ".brightspace-mcp")
        )
        self.headless: bool = os.environ.get("BRIGHTSPACE_HEADLESS", "false").lower() == "true"
        self.lp_version: str = "1.57"
        self.le_version: str = "1.92"
        self.download_dir: Path = Path(
            os.environ.get("BRIGHTSPACE_DOWNLOAD_DIR", self.session_dir / "downloads")
        )

        self.session_dir.mkdir(parents=True, exist_ok=True)

    @property
    def storage_state_path(self) -> Path:
        return self.session_dir / "storage_state.json"

    @property
    def cookie_age_seconds(self) -> float | None:
        """Seconds since cookies were last saved. None if no saved state."""
        if not self.storage_state_path.exists():
            return None
        return time.time() - self.storage_state_path.stat().st_mtime

    # Standard Microsoft AAD login selectors (stable across tenants)
    MS_EMAIL_INPUT = 'input[type="email"][name="loginfmt"]'
    MS_PASSWORD_INPUT = 'input[type="password"][name="passwd"]'
    MS_SUBMIT_BUTTON = "input[type='submit']#idSIButton9"
    MS_STAY_SIGNED_IN_NO = "input[type='button']#idBtn_Back"
