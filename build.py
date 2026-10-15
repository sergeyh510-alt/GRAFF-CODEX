# -*- coding: utf-8 -*-
"""Build GRAFF into a single runnable exe (PyInstaller).

Usage (from the project folder):

    pip install pyinstaller
    python build.py                 # one-file GRAFF.exe in dist/
    python build.py --onedir        # folder build (starts faster)
    python build.py --debug         # also keep a console window (default: on)
    python build.py --no-install    # never try to install PyInstaller

The exe is a *console* application: it starts a local server on
http://127.0.0.1:8756, opens the browser and streams the live graph.  It bundles
the whole ``static/`` tree (html/css/js + vis-network / three / 3d-force-graph),
so it runs with no project folder next to it.

Writable state (journal, snapshots, state.json) is created next to the exe in
``runtime/`` -- or in ``%LOCALAPPDATA%/GRAFF`` when that folder is not writable.
Nothing is ever written inside the exe.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

APP_ENTRY = "run.py"
SPEC_FILE = "GRAFF.spec"
DEFAULT_NAME = "GRAFF"

# The assets that must travel inside the exe, otherwise the frozen app cannot
# find its html/css/js at runtime (see graff/paths.py resource_dir()).
DATA_DIRS = ["static"]

# Copied next to the exe after a --onedir build so the folder is self-contained.
COPY_AFTER = ["README.md", SPEC_FILE]


def hr() -> None:
    print("=" * 66)


def ensure_pyinstaller(allow_install: bool = True) -> bool:
    try:
        import PyInstaller  # noqa: F401
        print("[ok] PyInstaller найден")
        return True
    except ImportError:
        if not allow_install:
            print("[!!] PyInstaller не установлен (запущено с --no-install)")
            return False
        print("[..] PyInstaller не найден — устанавливаю в текущий интерпретатор...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"])
            print("[ok] PyInstaller установлен")
            return True
        except subprocess.CalledProcessError as exc:
            print("[!!] Не удалось установить PyInstaller: %s" % exc)
            return False


def check_assets() -> bool:
    ok = True
    for folder in DATA_DIRS:
        if not os.path.isdir(folder):
            print("[!!] нет папки %s — exe соберётся без статики" % folder)
            ok = False
    for name in ("static/index.html", "static/js/app.js", "static/js/boot.js",
                 "static/vendor/vis-network.min.js", "static/vendor/three.min.js",
                 "static/vendor/3d-force-graph.min.js"):
        if not os.path.exists(name):
            print("[!!] отсутствует %s" % name)
            ok = False
    if ok:
        print("[ok] статика на месте (html/css/js + vendor-библиотеки)")
    return ok


def clean(keep_build: bool = False) -> None:
    for folder in (["dist"] + ([] if keep_build else ["build"])):
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            print("[..] удалено: %s/" % folder)


def build_command(name: str, onefile: bool, console: bool) -> list:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean"]
    cmd += ["--name", name]
    # Bundle the static assets exactly where graff/paths.py expects them.
    sep = ";" if os.name == "nt" else ":"
    for folder in DATA_DIRS:
        cmd += ["--add-data", "%s%s%s" % (folder, sep, folder)]
    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")
    cmd.append("--console" if console else "--windowed")
    cmd.append(APP_ENTRY)
    return cmd


def post_build(name: str, onefile: bool) -> str:
    dist = os.path.abspath("dist")
    if onefile:
        exe = os.path.join(dist, "%s.exe" % name)
        if os.path.exists(exe):
            return exe
        return ""
    target_dir = os.path.join(dist, name)
    for filename in COPY_AFTER:
        if os.path.exists(filename):
            shutil.copy2(filename, os.path.join(target_dir, filename))
    exe = os.path.join(target_dir, "%s.exe" % name)
    return exe if os.path.exists(exe) else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка GRAFF в exe (PyInstaller)")
    parser.add_argument("--name", default=DEFAULT_NAME, help="имя итогового exe (без .exe)")
    parser.add_argument("--onedir", action="store_true", help="собрать папкой вместо одного файла")
    parser.add_argument("--debug", "--console", dest="debug", action="store_true",
                        help="оставить консоль (режим отладки); консоль включена и по умолчанию")
    parser.add_argument("--no-clean", action="store_true", help="не удалять build/")
    parser.add_argument("--no-install", action="store_true", help="не ставить PyInstaller автоматически")
    args = parser.parse_args()

    hr()
    print(" GRAFF — сборка одного exe")
    hr()
    print(" интерпретатор : %s" % sys.executable)
    print(" приложение    : %s" % APP_ENTRY)
    print(" имя exe       : %s" % args.name)
    print(" режим         : %s" % ("ОДИН ФАЙЛ" if not args.onedir else "ПАПКА"))
    hr()

    if not os.path.exists(APP_ENTRY):
        print("[!!] %s не найден — запустите build.py из папки проекта." % APP_ENTRY)
        return 1

    check_assets()
    if not ensure_pyinstaller(allow_install=not args.no_install):
        return 1

    clean(keep_build=args.no_clean)
    cmd = build_command(args.name, onefile=not args.onedir, console=True)
    print("[..] команда сборки:\n     %s\n" % " ".join(cmd))
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as exc:
        print("\n[!!] Ошибка сборки: %s" % exc)
        return 1

    hr()
    print(" [ok] СБОРКА ЗАВЕРШЕНА")
    hr()
    exe = post_build(args.name, onefile=not args.onedir)
    if exe and os.path.exists(exe):
        print(" exe: %s  (%.1f МБ)" % (exe, os.path.getsize(exe) / 1024.0 / 1024.0))
    print("\n Запуск:")
    print("   1) откройте папку dist и запустите %s.exe" % args.name)
    print("   2) браузер откроется сам: http://127.0.0.1:8756/")
    print("   3) изменяемое состояние (runtime/state.json, journal.jsonl, snapshots/)")
    print("      появится рядом с exe, либо в %%LOCALAPPDATA%%\\GRAFF\\runtime")
    print("   4) для другого порта:  %s.exe --port 8770" % args.name)
    hr()
    return 0


if __name__ == "__main__":
    sys.exit(main())