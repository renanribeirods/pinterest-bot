"""Baixa as fontes (Google Fonts, licença OFL) usadas pelos templates para templates/fonts/."""
from __future__ import annotations

import sys
from pathlib import Path

import requests

FONTS_DIR = Path(__file__).resolve().parent.parent / "templates" / "fonts"
BASE = "https://github.com/google/fonts/raw/main/ofl"
FONTS = {
    "Anton-Regular.ttf": f"{BASE}/anton/Anton-Regular.ttf",
    "Poppins-Bold.ttf": f"{BASE}/poppins/Poppins-Bold.ttf",
    "Poppins-SemiBold.ttf": f"{BASE}/poppins/Poppins-SemiBold.ttf",
    "Poppins-Regular.ttf": f"{BASE}/poppins/Poppins-Regular.ttf",
    "DMSerifDisplay-Regular.ttf": f"{BASE}/dmserifdisplay/DMSerifDisplay-Regular.ttf",
    "PatrickHand-Regular.ttf": f"{BASE}/patrickhand/PatrickHand-Regular.ttf",
    "BebasNeue-Regular.ttf": f"{BASE}/bebasneue/BebasNeue-Regular.ttf",
}


def main() -> int:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    missing = 0
    for name, url in FONTS.items():
        dest = FONTS_DIR / name
        if dest.exists() and dest.stat().st_size > 10_000:
            continue
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
            print(f"ok   {name}")
        except requests.RequestException as exc:
            missing += 1
            print(f"FAIL {name}: {exc} (o gerador usará a fonte padrão do Pillow)")
    return 0 if missing < len(FONTS) else 1


if __name__ == "__main__":
    sys.exit(main())
