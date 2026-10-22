"""Install per-user design file associations and LDS preview thumbnails."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


LDS_MIME = "application/x-leonardo-design"
DESIGN_MIME = "application/x-verona-design"


def _exec_argument(value):
    value = str(value).replace("%", "%%")
    if all(character.isalnum() or character in "/._-" for character in value):
        return value
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value.replace("\\", "\\\\") + '"'


def install_desktop():
    python = _exec_argument(sys.executable)
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home()/".local/share"))
    applications = data_home/"applications"
    mime = data_home/"mime"
    thumbnailers = data_home/"thumbnailers"
    for directory in (applications, mime/"packages", thumbnailers):
        directory.mkdir(parents=True, exist_ok=True)
    for language, name, comment in (
            ("zh-TW", "Verona（繁體中文）", "設計、編輯與切割"),
            ("en", "Verona", "Offline design, editing and cutting")):
        (applications/f"verona-{language}.desktop").write_text(
            "[Desktop Entry]\nVersion=1.0\nType=Application\n"
            f"Name={name}\nComment={comment}\n"
            f"Exec={python} -m verona.desktop_integration --launch {language.replace('-', '_')} %f\n"
            "Icon=applications-graphics\nTerminal=false\n"
            "Categories=Graphics;2DGraphics;VectorGraphics;\nStartupNotify=true\n"
            f"MimeType={DESIGN_MIME};{LDS_MIME};\n", encoding="utf-8")
    (mime/"packages/verona.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
        f'  <mime-type type="{DESIGN_MIME}">\n'
        '    <comment>Verona design</comment>\n'
        '    <comment xml:lang="zh_TW">Verona 設計文件</comment>\n'
        '    <glob pattern="*.jdesign"/>\n'
        '    <sub-class-of type="application/json"/>\n'
        '    <generic-icon name="x-office-drawing"/>\n'
        '  </mime-type>\n'
        f'  <mime-type type="{LDS_MIME}">\n'
        '    <comment>Leonardo Design Studio document</comment>\n'
        '    <comment xml:lang="zh_TW">Leonardo 設計文件</comment>\n'
        '    <glob pattern="*.lds"/>\n'
        '    <sub-class-of type="application/zip"/>\n'
        '    <generic-icon name="x-office-drawing"/>\n'
        '  </mime-type>\n</mime-info>\n', encoding="utf-8")
    (thumbnailers/"verona-lds.thumbnailer").write_text(
        "[Thumbnailer Entry]\n"
        f"TryExec={sys.executable}\n"
        f"Exec={python} -m verona.thumbnailer %i %o %s\n"
        f"MimeType={LDS_MIME};\n", encoding="utf-8")
    subprocess.run(["update-mime-database", str(mime)], check=True)
    subprocess.run(["update-desktop-database", str(applications)], check=True)
    for mime_type in (DESIGN_MIME, LDS_MIME):
        subprocess.run(["xdg-mime", "default", "verona-en.desktop", mime_type], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install per-user Verona desktop integration")
    parser.add_argument("--launch", choices=("en", "zh_TW"))
    parser.add_argument("document", nargs="?")
    args = parser.parse_args(argv)
    if args.launch:
        from .ui import main as launch
        return launch(["--lang", args.launch, *(["--open", args.document] if args.document else [])])
    install_desktop()
    print("Installed JDESIGN/LDS file associations and LDS thumbnails for the current user.")
    print("First installation: restart your file manager to reload thumbnailers (verified with Nemo).")


if __name__ == "__main__":
    main()
