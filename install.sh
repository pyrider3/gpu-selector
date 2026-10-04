#!/usr/bin/env bash
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
target_dir="${XDG_DATA_HOME:-$HOME/.local/share}/gpu-selector"
app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$target_dir" "$app_dir"
cp "$source_dir/gpu_selector.py" "$target_dir/gpu_selector.py"
cp "$source_dir/README.md" "$target_dir/README.md"
chmod +x "$target_dir/gpu_selector.py"
python - "$target_dir" "$app_dir" <<'PY'
import pathlib,sys
target=pathlib.Path(sys.argv[1]);apps=pathlib.Path(sys.argv[2])
def quote(s):
 return '"'+str(s).replace('\\','\\\\').replace('"','\\"').replace('$','\\$').replace('`','\\`').replace('%','%%')+'"'
(apps/'gpu-selector.desktop').write_text('[Desktop Entry]\nType=Application\nName=显卡选择器\nName[en]=GPU Selector\nComment=选择应用使用的 GPU\nExec='+quote(sys.executable)+' '+quote(target/'gpu_selector.py')+'\nIcon=video-display\nTerminal=false\nCategories=System;Utility;\nStartupNotify=true\n')
PY
printf '%s\n' '安装完成。在应用菜单搜索「显卡选择器」。不会开机自启，也不需要 sudo。'
