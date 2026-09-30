"""Build local Tailwind utilities and Lucide navigation icons without Node.js."""

from pathlib import Path
import subprocess
from urllib.parse import quote
from urllib.request import urlretrieve


ROOT = Path(__file__).resolve().parent
ICONS = {
    "space_dashboard": "layout-dashboard", "health_and_safety": "activity",
    "monitoring": "circle-dollar-sign", "fact_check": "layers",
    "visibility": "cpu", "admin_panel_settings": "shield-check",
    "assignment": "git-pull-request", "auto_fix_high": "wrench",
}


def main():
    tools = ROOT / ".tools"
    assets = ROOT / "assets"
    tools.mkdir(exist_ok=True)
    assets.mkdir(exist_ok=True)
    compiler = tools / "tailwindcss.exe"
    if not compiler.exists():
        urlretrieve("https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.0/tailwindcss-windows-x64.exe", compiler)
    build = subprocess.run([str(compiler), "-i", str(ROOT / "components" / "enterprise.css"),
                            "--minify"], check=True, cwd=ROOT, capture_output=True, encoding="utf-8")
    (assets / "pilot.css").write_text(build.stdout, encoding="utf-8")
    icon_styles = []
    for key, name in ICONS.items():
        target = assets / f"lucide-{name}.svg"
        if not target.exists():
            urlretrieve(f"https://unpkg.com/lucide-static@0.468.0/icons/{name}.svg", target)
        svg = target.read_text(encoding="utf-8").replace('stroke="currentColor"', 'stroke="#c7d2e0"')
        icon_styles.append(f'.st-key-nav_{key} button::before {{ content:""; width:18px; height:18px; flex:0 0 18px; background:center/contain no-repeat url("data:image/svg+xml,{quote(svg)}"); }}')
        icon_styles.append(f'.st-key-nav_{key} [data-testid="stIconMaterial"] {{ display:none; }}')
    with (assets / "pilot.css").open("a", encoding="utf-8") as stylesheet:
        stylesheet.write("\n".join(icon_styles))
    license_file = assets / "lucide-LICENSE"
    if not license_file.exists():
        urlretrieve("https://unpkg.com/lucide-static@0.468.0/LICENSE", license_file)
    print("Built assets/pilot.css with Tailwind CSS 4.1.0 and Lucide 0.468.0")


if __name__ == "__main__":
    main()