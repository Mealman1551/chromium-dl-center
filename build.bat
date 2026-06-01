@echo off
Title compiling chr.py
echo Compiling chr.py with Nuitka...

python -m nuitka --standalone --output-dir=dist --remove-output --windows-console-mode=disable --enable-plugin=tk-inter --include-package=requests --windows-icon-from-ico=assets/chr.ico src/chr.py