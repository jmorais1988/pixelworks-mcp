#!/usr/bin/env python
"""Re-apply the pixelworks headless guard to the Retro Diffusion
extension entry script (extension.lua). Idempotent: skips if marker present.
Run after Retro Diffusion extension updates.
"""
import os
from pathlib import Path

_DEFAULT_EXT_DIR = (
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    / "Aseprite" / "extensions" / "RetroDiffusion"
)
EXT = Path(os.environ.get("RD_EXTENSION_DIR") or _DEFAULT_EXT_DIR) / "extension.lua"

GUARD = '''-- [retro-diffusion-mcp headless guard]
-- When Aseprite is launched headlessly by pixelworks (env
-- RD_MCP_HEADLESS=1), suppress every os.execute shell-out from this extension:
-- on Windows each one flashes a visible cmd console window, and the Unix-only
-- branches print "'rm' is not recognized as an internal or external command".
-- GUI sessions are unaffected (env var absent). RE-APPLY AFTER RD UPDATES:
-- run pixelworks/reapply_headless_patch.py
if os.getenv("RD_MCP_HEADLESS") == "1" then
    os.execute = function(...) return 0 end
end

'''

def main() -> None:
    if not EXT.exists():
        raise SystemExit(
            f"extension.lua not found at {EXT}\n"
            "Set RD_EXTENSION_DIR to the Retro Diffusion extension folder."
        )
    src = EXT.read_text(encoding="utf-8")
    if "retro-diffusion-mcp headless guard" in src:
        print("guard already present:", EXT)
        return
    EXT.write_text(GUARD + src, encoding="utf-8")
    print("guard inserted at top of", EXT)

if __name__ == "__main__":
    main()
