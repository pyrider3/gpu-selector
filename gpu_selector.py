#!/usr/bin/env python3
"""Per-application GPU launcher for Linux. No privileged operations."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'gpu-selector'
APPS = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'applications'
SELECT_VARS = ('CUDA_VISIBLE_DEVICES', 'DRI_PRIME', 'MESA_VK_DEVICE_SELECT',
               'MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE', '__NV_PRIME_RENDER_OFFLOAD',
               '__NV_PRIME_RENDER_OFFLOAD_PROVIDER', '__GLX_VENDOR_LIBRARY_NAME',
               '__VK_LAYER_NV_optimus', 'VK_ICD_FILENAMES', 'VK_DRIVER_FILES',
               'GGML_VK_VISIBLE_DEVICES')


def command(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=8, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


@dataclass
class GPU:
    key: str
    name: str
    pci: str
    vendor: str
    device: str
    driver: str
    node: str
    uuid: str = ''
    memory: str = ''

    @property
    def label(self):
        name = self.name.replace('NVIDIA GeForce ', '').replace('Intel Corporation ', '').replace('Meteor Lake-P [Intel Arc Graphics]', 'Intel Arc Graphics')
        return name + (f' · {self.memory}' if self.memory else ' · 核显')


def detect_gpus():
    nvidia = {}
    out = command(['nvidia-smi', '--query-gpu=pci.bus_id,name,uuid,memory.total', '--format=csv,noheader,nounits'])
    for row in csv.reader(io.StringIO(out)):
        if len(row) == 4:
            bus, name, uuid, memory = [s.strip() for s in row]
            nvidia[bus[-12:].lower()] = (name, uuid, f'{int(memory) / 1024:.1f} GB')
    result = []
    for path in sorted(Path('/sys/class/drm').glob('card[0-9]*')):
        if not path.name[4:].isdigit():
            continue
        dev = path / 'device'
        try:
            pci = dev.resolve().name.lower()
            vendor = (dev / 'vendor').read_text().strip().removeprefix('0x')
            device = (dev / 'device').read_text().strip().removeprefix('0x')
            driver = (dev / 'driver').resolve().name
        except OSError:
            continue
        line = command(['lspci', '-s', pci]).strip()
        name = line.split(': ', 1)[-1].split(' (rev ')[0] if line else f'{vendor}:{device}'
        uuid, memory = '', ''
        if pci in nvidia:
            name, uuid, memory = nvidia[pci]
        result.append(GPU(uuid or pci, name, pci, vendor, device, driver,
                          str(path / 'device'), uuid, memory))
    return sorted(result, key=lambda gpu: (gpu.vendor != '8086', gpu.pci))


def gpu_environment(gpu, mode):
    if mode not in ('combined', 'vulkan', 'cuda', 'opengl'):
        raise ValueError('未知启动模式')
    env = {}
    if mode in ('combined', 'vulkan'):
        if not Path('/usr/share/vulkan/implicit_layer.d/VkLayer_MESA_device_select.json').exists():
            raise ValueError('缺少 Mesa Vulkan 设备选择层，请安装 vulkan-mesa-implicit-layers')
        env['MESA_VK_DEVICE_SELECT'] = f'{gpu.vendor}:{gpu.device}!'
        env['MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE'] = '1'
    if mode in ('combined', 'cuda'):
        # Integrated GPU is not a CUDA device; -1 intentionally requests CPU.
        env['CUDA_VISIBLE_DEVICES'] = gpu.uuid or '-1'
        if mode == 'cuda':
            env['OLLAMA_VULKAN'] = '0'
    if mode == 'opengl':
        if gpu.vendor == '10de':
            raise ValueError('当前 Niri/Wayland 无法保证 OpenGL 精确选择某张 NVIDIA 卡。请使用应用内选卡或 Vulkan 模式。')
        env['DRI_PRIME'] = 'pci-' + gpu.pci.replace(':', '_').replace('.', '_')
        env['__GLX_VENDOR_LIBRARY_NAME'] = 'mesa'
    return env


def child_environment(gpu, mode):
    env = os.environ.copy()
    for key in SELECT_VARS:
        env.pop(key, None)
    env.update(gpu_environment(gpu, mode))
    return env


def settings():
    try:
        return json.loads((CONFIG / 'preferences.json').read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_settings(data):
    CONFIG.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG / 'preferences.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(CONFIG / 'preferences.json')


def desktop_quote(value):
    # Freedesktop Exec has its own quoting rules, independent of shell quoting.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


def save_preference(app, gpu, mode):
    gpu_environment(gpu, mode)  # Validate before making changes.
    ident = app.get_id()
    if not ident or Path(ident).name != ident or not ident.endswith('.desktop'):
        raise ValueError('此应用没有可保存的桌面入口')
    data = settings()
    target = APPS / ident
    prior = data.get(ident, {})
    original = prior.get('source') or app.get_filename()
    if not original or not Path(original).exists():
        raise ValueError('原始桌面入口不存在')
    if not prior:
        backup_dir = CONFIG / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        had_override = target.exists()
        if had_override:
            (backup_dir / ident).write_bytes(target.read_bytes())
        prior = {'source': original, 'had_override': had_override}
        # Existing user entry is copied to a stable source outside applications.
        source_dir = CONFIG / 'sources'
        source_dir.mkdir(parents=True, exist_ok=True)
        source = source_dir / ident
        source.write_bytes(Path(original).read_bytes())
        prior['source'] = str(source)
    prior.update(gpu=gpu.key, mode=mode)
    data[ident] = prior
    from gi.repository import GLib
    keyfile = GLib.KeyFile()
    keyfile.load_from_file(prior['source'], GLib.KeyFileFlags.KEEP_COMMENTS | GLib.KeyFileFlags.KEEP_TRANSLATIONS)
    exe = ' '.join(desktop_quote(x) for x in [sys.executable, ROOT / 'gpu_selector.py', '--launch-id', ident])
    keyfile.set_string('Desktop Entry', 'Exec', exe + ' -- %U')
    keyfile.set_boolean('Desktop Entry', 'DBusActivatable', False)
    # Runner uses the original entry's terminal settings; avoid a double terminal.
    keyfile.set_boolean('Desktop Entry', 'Terminal', False)
    keyfile.set_string('Desktop Entry', 'X-GPU-Selector', 'true')
    if 'Actions' in keyfile.get_keys('Desktop Entry')[0]:
        keyfile.remove_key('Desktop Entry', 'Actions')
    APPS.mkdir(parents=True, exist_ok=True)
    write_settings(data)
    target.write_text(keyfile.to_data()[0])


def restore_preference(ident):
    data = settings()
    prior = data.get(ident)
    if not prior:
        return
    target = APPS / ident
    if target.exists() and 'X-GPU-Selector=true' not in target.read_text():
        raise ValueError('桌面入口已被其他软件修改，未覆盖。请手动检查。')
    if prior.get('had_override'):
        target.write_bytes((CONFIG / 'backups' / ident).read_bytes())
    elif target.exists():
        target.unlink()
    del data[ident]
    write_settings(data)


def launch_desktop(source, gpu, mode, uris=None):
    from gi.repository import Gio, GLib
    keyfile = GLib.KeyFile()
    keyfile.load_from_file(str(source), GLib.KeyFileFlags.NONE)
    keyfile.set_boolean('Desktop Entry', 'DBusActivatable', False)
    app = Gio.DesktopAppInfo.new_from_keyfile(keyfile)
    if not app:
        raise ValueError('无法读取应用入口')
    context = Gio.AppLaunchContext()
    for key in SELECT_VARS:
        context.unsetenv(key)
    for key, value in gpu_environment(gpu, mode).items():
        context.setenv(key, value)
    app.launch_uris(uris or [], context)


def process_rows(gpus):
    rows = {}
    # nvidia-smi pmon covers graphics and compute, including desktop clients.
    indexes = {}
    for row in csv.reader(io.StringIO(command(['nvidia-smi', '--query-gpu=index,uuid', '--format=csv,noheader']))):
        if len(row) == 2:
            indexes[row[0].strip()] = row[1].strip()
    for line in command(['nvidia-smi', 'pmon', '-c', '1']).splitlines():
        cols = line.split()
        if len(cols) < 4 or not cols[0].isdigit() or not cols[1].isdigit():
            continue
        pid = int(cols[1])
        gpu = next((g for g in gpus if g.uuid == indexes.get(cols[0])), None)
        if gpu:
            rows.setdefault(pid, set()).add(gpu.name)
    # DRM fdinfo also discovers Intel clients. Open DRM handles do not prove load.
    devices = {}
    for g in gpus:
        for n in Path(g.node).glob('drm/*'):
            devices['/dev/dri/' + n.name] = g.name
    for p in Path('/proc').glob('[0-9]*'):
        try:
            if p.stat().st_uid != os.getuid():
                continue
            if (p / 'cmdline').read_bytes():
                rows.setdefault(int(p.name), set())
            for fd in (p / 'fd').iterdir():
                try:
                    target = os.readlink(fd)
                    if target in devices:
                        rows.setdefault(int(p.name), set()).add(devices[target])
                except OSError:
                    pass
        except OSError:
            continue
    result = []
    for pid, cards in rows.items():
        try:
            name = Path(f'/proc/{pid}/comm').read_text().strip()
            args = [s.decode(errors='replace') for s in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if s]
            result.append((pid, name, ', '.join(sorted(cards)) or '未发现 GPU 句柄（可能是 CPU 程序或检测权限不足）', args))
        except OSError:
            pass
    return sorted(result, key=lambda row: row[1].lower())


def main_gui():
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, Gio, GLib, Gdk, Pango

    class Window(Gtk.ApplicationWindow):
        def __init__(self, application):
            super().__init__(application=application, title='显卡选择器')
            self.set_default_size(1120, 820)
            self.gpus = []
            self.apps = []
            self.selected_app = None
            self.selected_process = None
            header = Gtk.HeaderBar()
            header.set_title_widget(Gtk.Label(label='GPU SELECTOR'))
            header.add_css_class('app-header')
            refresh = Gtk.Button(label='刷新设备')
            refresh.set_icon_name('view-refresh-symbolic')
            refresh.set_tooltip_text('重新检测显卡和运行中的应用')
            refresh.connect('clicked', lambda *_: self.refresh())
            header.pack_end(refresh)
            self.set_titlebar(header)
            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            root.add_css_class('surface')
            for setter in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
                getattr(root, setter)(24)
            self.set_child(root)
            eyebrow = Gtk.Label(label='你的设备 · 你的选择', xalign=0)
            eyebrow.add_css_class('eyebrow')
            root.append(eyebrow)
            title = Gtk.Label(label='为应用选择合适的显卡', xalign=0)
            title.add_css_class('hero')
            root.append(title)
            subtitle = Gtk.Label(label='轻量应用交给核显，游戏与 AI 交给独显。为每次启动安排好性能。', xalign=0, wrap=True)
            subtitle.add_css_class('muted')
            root.append(subtitle)
            self.cards = Gtk.Box(spacing=12, homogeneous=True)
            root.append(self.cards)
            controls = Gtk.Box(spacing=12)
            controls.add_css_class('control-panel')
            controls.append(Gtk.Label(label='目标显卡'))
            self.gpu_pick = Gtk.DropDown.new_from_strings(['正在检测…'])
            self.gpu_pick.set_hexpand(True)
            self.gpu_pick.connect('notify::selected', lambda *_: self.update_cards())
            controls.append(self.gpu_pick)
            self.mode = Gtk.DropDown.new_from_strings(['Vulkan + CUDA', 'Vulkan / 游戏 / 核显 AI', 'CUDA / AI（核显为 CPU）', 'OpenGL（仅核显）'])
            controls.append(Gtk.Label(label='启动模式'))
            controls.append(self.mode)
            root.append(controls)
            note = Gtk.Label(label='运行中的程序通常需要退出后重新启动。Vulkan/CUDA 可指定设备；OpenGL 无法在当前 Wayland 环境精确区分两张 NVIDIA 卡。', xalign=0, wrap=True)
            note.add_css_class('muted')
            root.append(note)
            stack = Gtk.Stack(vexpand=True)
            stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
            switcher = Gtk.StackSwitcher(stack=stack, halign=Gtk.Align.START)
            switcher.add_css_class('tabs')
            root.append(switcher)
            root.append(stack)
            self.search = Gtk.SearchEntry(placeholder_text='搜索已安装的应用…')
            self.search.connect('search-changed', lambda *_: self.populate_apps())
            apps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            apps_box.append(self.search)
            self.app_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
            self.app_list.add_css_class('app-list')
            self.app_list.connect('row-selected', self.app_selected)
            scroll = Gtk.ScrolledWindow(vexpand=True, child=self.app_list)
            scroll.add_css_class('list-panel')
            apps_box.append(scroll)
            actions = Gtk.Box(spacing=10)
            actions.add_css_class('action-bar')
            self.selected_label = Gtk.Label(label='选择一个应用，开始分配显卡', xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            self.selected_label.add_css_class('muted')
            actions.append(self.selected_label)
            for label, callback in [('恢复默认', self.restore_app), ('保存偏好', self.save_app), ('启动应用  →', self.launch_app)]:
                b = Gtk.Button(label=label)
                b.connect('clicked', callback)
                actions.append(b)
            actions.get_last_child().add_css_class('suggested-action')
            apps_box.append(actions)
            stack.add_titled(apps_box, 'apps', '已安装应用')
            proc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            proc_box.append(Gtk.Label(label='显示本用户的运行中进程及可检测到的 GPU。持有设备句柄不代表正在高负载运算。', wrap=True, xalign=0))
            self.proc_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
            self.proc_list.connect('row-selected', lambda _, row: setattr(self, 'selected_process', row))
            proc_box.append(Gtk.ScrolledWindow(vexpand=True, child=self.proc_list))
            b = Gtk.Button(label='把所选进程的命令放入启动器')
            b.connect('clicked', lambda *_: self.use_process(stack))
            proc_box.append(b)
            stack.add_titled(proc_box, 'processes', '运行中')
            custom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            custom.append(Gtk.Label(label='输入程序和参数；不经过 shell。已有后台服务、单实例程序可能沿用原进程的显卡，需要先自行退出。', xalign=0, wrap=True))
            self.cmd = Gtk.Entry(placeholder_text='例如：python /路径/程序.py 或 ollama serve')
            custom.append(self.cmd)
            self.cwd = Gtk.Entry(placeholder_text='工作目录（可选）')
            custom.append(self.cwd)
            b = Gtk.Button(label='启动命令')
            b.add_css_class('suggested-action')
            b.connect('clicked', self.launch_command)
            custom.append(b)
            custom.append(Gtk.Label(label='Ollama：显卡变量应设置在 ollama serve 上，而不是 ollama run。已有 systemd 服务需先停止。此工具不修改服务或强制关闭进程。', xalign=0, wrap=True))
            stack.add_titled(custom, 'command', '自定义命令')
            self.status = Gtk.Label(label='就绪', xalign=0, wrap=True)
            self.status.add_css_class('status')
            root.append(self.status)
            self.refresh()

        def feedback(self, message):
            self.status.set_text(message)

        def selection(self):
            index = self.gpu_pick.get_selected()
            if index >= len(self.gpus):
                raise ValueError('没有可用的目标显卡，请刷新设备')
            return self.gpus[index], ['combined', 'vulkan', 'cuda', 'opengl'][self.mode.get_selected()]

        def clear(self, box):
            while box.get_first_child():
                box.remove(box.get_first_child())

        def refresh(self):
            old_index = self.gpu_pick.get_selected()
            old_key = self.gpus[old_index].key if old_index < len(self.gpus) else None
            self.gpus = detect_gpus()
            self.gpu_pick.set_model(Gtk.StringList.new([g.label for g in self.gpus]))
            if old_key:
                for i, g in enumerate(self.gpus):
                    if g.key == old_key:
                        self.gpu_pick.set_selected(i)
            self.clear(self.cards)
            for i, g in enumerate(self.gpus):
                card = Gtk.Button()
                card.add_css_class('gpu-card')
                content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                top = Gtk.Box(spacing=8)
                icon = Gtk.Image.new_from_icon_name('video-display-symbolic')
                icon.set_pixel_size(22)
                top.append(icon)
                kind = '核显' if g.vendor == '8086' else ('外接独显' if '5080' in g.name else '内置独显')
                badge = Gtk.Label(label=kind, xalign=0, hexpand=True)
                badge.add_css_class('device-kind')
                top.append(badge)
                online = Gtk.Label(label='● 在线')
                online.add_css_class('online')
                top.append(online)
                content.append(top)
                short_name = g.name.replace('NVIDIA GeForce ', '').replace('Intel Corporation ', '').replace('Meteor Lake-P [Intel Arc Graphics]', 'Intel Arc Graphics')
                name = Gtk.Label(label=short_name, xalign=0, wrap=True)
                name.add_css_class('card-title')
                content.append(name)
                memory = Gtk.Label(label=g.memory or '共享系统内存', xalign=0)
                memory.add_css_class('memory')
                content.append(memory)
                detail = Gtk.Label(label=f'{g.driver} · {g.pci}', xalign=0)
                detail.add_css_class('device-detail')
                content.append(detail)
                card.set_child(content)
                card.connect('clicked', lambda _, index=i: self.gpu_pick.set_selected(index))
                self.cards.append(card)
            self.update_cards()
            self.apps = sorted([a for a in Gio.AppInfo.get_all() if isinstance(a, Gio.DesktopAppInfo) and a.should_show()], key=lambda a: a.get_display_name().lower())
            self.populate_apps()
            self.clear(self.proc_list)
            for pid, name, cards, args in process_rows(self.gpus):
                row = Gtk.ListBoxRow()
                row.args = args
                try:
                    row.cwd = os.readlink(f'/proc/{pid}/cwd')
                except OSError:
                    row.cwd = ''
                row.set_child(Gtk.Label(label=f'{name}  ·  PID {pid}\n{cards}', xalign=0, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12, wrap=True))
                self.proc_list.append(row)
            self.feedback(f'已检测到 {len(self.gpus)} 张显卡。保存偏好只影响该应用以后的菜单启动。')

        def update_cards(self):
            card = self.cards.get_first_child()
            index = 0
            while card:
                if index == self.gpu_pick.get_selected():
                    card.add_css_class('selected-gpu')
                else:
                    card.remove_css_class('selected-gpu')
                card = card.get_next_sibling()
                index += 1

        def populate_apps(self):
            self.clear(self.app_list)
            term = self.search.get_text().lower()
            saved = settings()
            for a in self.apps:
                if term not in (a.get_display_name() + ' ' + (a.get_id() or '')).lower():
                    continue
                row = Gtk.ListBoxRow()
                row.app = a
                box = Gtk.Box(spacing=12, margin_top=9, margin_bottom=9, margin_start=12, margin_end=12)
                if a.get_icon():
                    icon = Gtk.Image.new_from_gicon(a.get_icon())
                    icon.set_pixel_size(32)
                    box.append(icon)
                info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
                name = Gtk.Label(label=a.get_display_name(), xalign=0, ellipsize=Pango.EllipsizeMode.END)
                name.add_css_class('app-name')
                info.append(name)
                pref = saved.get(a.get_id())
                detail = Gtk.Label(label='已保存专属显卡偏好' if pref else '沿用系统显卡选择', xalign=0, ellipsize=Pango.EllipsizeMode.END)
                detail.add_css_class('app-detail')
                info.append(detail)
                box.append(info)
                pref = saved.get(a.get_id())
                text = '系统默认'
                if pref:
                    text = next((g.name.replace('NVIDIA GeForce ', '') for g in self.gpus if g.key == pref['gpu']), '已保存设备 · 当前离线')
                badge = Gtk.Label(label=text, xalign=1)
                badge.add_css_class('preference-badge' if pref else 'default-badge')
                box.append(badge)
                row.set_child(box)
                self.app_list.append(row)

        def app_selected(self, _, row):
            self.selected_app = getattr(row, 'app', None)
            self.selected_label.set_text(self.selected_app.get_display_name() if self.selected_app else '选择一个应用，开始分配显卡')
            if self.selected_app:
                pref = settings().get(self.selected_app.get_id())
                if pref:
                    for i, gpu in enumerate(self.gpus):
                        if gpu.key == pref['gpu']:
                            self.gpu_pick.set_selected(i)
                    self.mode.set_selected(['combined', 'vulkan', 'cuda', 'opengl'].index(pref['mode']))

        def require_app(self):
            if not self.selected_app:
                raise ValueError('请先选择一个应用')
            return self.selected_app

        def launch_app(self, *_):
            try:
                a = self.require_app()
                gpu, mode = self.selection()
                prior = settings().get(a.get_id(), {})
                launch_desktop(prior.get('source') or a.get_filename(), gpu, mode)
                self.feedback(f'已发出启动请求：{a.get_display_name()} → {gpu.name}。请在“运行中”刷新确认；已有单实例程序需先退出。')
            except Exception as e:
                self.feedback(str(e))

        def save_app(self, *_):
            try:
                a = self.require_app()
                gpu, mode = self.selection()
                save_preference(a, gpu, mode)
                self.feedback(f'已保存 {a.get_display_name()} → {gpu.name}；以后从应用菜单启动会应用。直接终端运行不受影响。')
                self.populate_apps()
            except Exception as e:
                self.feedback(str(e))

        def restore_app(self, *_):
            try:
                a = self.require_app()
                restore_preference(a.get_id())
                self.feedback('已恢复原来的应用菜单入口')
                self.populate_apps()
            except Exception as e:
                self.feedback(str(e))

        def use_process(self, stack):
            if self.selected_process:
                self.cmd.set_text(shlex.join(self.selected_process.args))
                self.cwd.set_text(self.selected_process.cwd)
                stack.set_visible_child_name('command')
                self.feedback('已复制原命令。请先自行退出旧进程；此工具不会强行终止程序。')

        def launch_command(self, *_):
            try:
                args = shlex.split(self.cmd.get_text())
                if not args:
                    raise ValueError('请输入启动命令')
                gpu, mode = self.selection()
                env = child_environment(gpu, mode)
                CONFIG.mkdir(parents=True, exist_ok=True)
                with (CONFIG / 'launch.log').open('ab') as log:
                    p = subprocess.Popen(args, cwd=self.cwd.get_text() or None, env=env, stdout=log, stderr=log, start_new_session=True)
                self.feedback(f'已启动 PID {p.pid} → {gpu.name}。输出保存在 {CONFIG / "launch.log"}')
            except Exception as e:
                self.feedback(str(e))

    app = Gtk.Application(application_id='local.codex.GpuSelector')
    def activate(application):
        if application.get_active_window():
            application.get_active_window().present()
            return
        css = Gtk.CssProvider()
        Gtk.Settings.get_default().set_property('gtk-theme-name', 'Adwaita')
        Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', False)
        css.load_from_data(b"""
            window, .surface { background: #f5f6fa; color: #22263a; font-size: 14px; }
            headerbar { background: #f5f6fa; box-shadow: none; border: none; }
            .app-header label { font-size: 11px; font-weight: 800; letter-spacing: 2px; color: #777e96; }
            .eyebrow { color: #6860cb; font-size: 12px; font-weight: 700; }
            .hero { font-size: 30px; font-weight: 800; letter-spacing: -1px; }
            .muted { color: #777e93; font-size: 13px; }
            button { border-radius: 10px; padding: 10px 16px; box-shadow: none; }
            button.suggested-action { background: #6558d9; color: white; border-color: #6558d9; font-weight: 700; }
            button.suggested-action:hover { background: #7669e6; }
            button.gpu-card { padding: 20px; border-radius: 18px; background: #ffffff; background-image: none; border: 2px solid #e7e9f1; color: #22263a; }
            button.gpu-card:hover { border-color: #c1bbea; background: #fbfaff; }
            button.selected-gpu { border-color: #7767e2; background: #f0edff; }
            .card-title { font-size: 19px; font-weight: 800; }
            .device-kind { font-size: 12px; font-weight: 600; color: #73798e; }
            .online { color: #258669; font-size: 11px; font-weight: 600; }
            .memory { font-size: 14px; font-weight: 600; color: #545a74; }
            .device-detail { color: #8a90a4; font-size: 11px; }
            .control-panel { padding: 14px 18px; border-radius: 14px; background: #eceef5; }
            dropdown > button { background: white; border: 1px solid #dde0eb; }
            .tabs button { border-radius: 9px; border: none; background: transparent; color: #777e93; font-weight: 600; }
            .tabs button:checked { background: #e7e2fc; color: #6150c8; }
            searchentry, entry { border-radius: 11px; padding: 9px 12px; border: 1px solid #dde0eb; background: white; }
            .list-panel { border-radius: 14px; border: 1px solid #e2e5ee; background: white; }
            .app-list { background: white; }
            .app-list row { padding: 2px 6px; border-bottom: 1px solid #f0f1f6; color: #22263a; }
            .app-list row:hover { background: #f7f6fd; }
            .app-list row:selected { background: #efecfc; color: #22263a; }
            .app-name { font-size: 14px; font-weight: 600; }
            .app-detail { font-size: 11px; color: #8b91a3; }
            .default-badge { color: #9096a7; font-size: 12px; }
            .preference-badge { padding: 6px 10px; background: #e9e5fb; border-radius: 8px; color: #6556b9; font-size: 12px; }
            .action-bar { padding-top: 6px; }
            .status { padding: 12px 16px; border-radius: 11px; background: #eaf0ef; color: #4b6c63; font-size: 12px; }
        """)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        window = Window(application)
        window.present()
        if os.environ.get('GPU_SELECTOR_SCREENSHOT'):
            def capture():
                try:
                    from gi.repository import Gsk, Graphene
                    paintable = Gtk.WidgetPaintable.new(window.get_child())
                    snapshot = Gtk.Snapshot()
                    paintable.snapshot(snapshot, window.get_child().get_width(), window.get_child().get_height())
                    node = snapshot.to_node()
                    renderer = Gsk.Renderer.new_for_surface(window.get_surface())
                    texture = renderer.render_texture(node, None)
                    texture.save_to_png(os.environ['GPU_SELECTOR_SCREENSHOT'])
                    renderer.unrealize()
                finally:
                    application.quit()
                return False
            GLib.timeout_add(2500, capture)
    app.connect('activate', activate)
    return app.run([sys.argv[0]])


def main():
    parser = argparse.ArgumentParser(description='Linux per-application GPU selector')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--launch-id')
    parser.add_argument('--gpu')
    parser.add_argument('--mode', default='combined', choices=['combined', 'vulkan', 'cuda', 'opengl'])
    parser.add_argument('--env', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.list:
        print(json.dumps([g.__dict__ for g in detect_gpus()], ensure_ascii=False, indent=2))
        return 0
    if args.launch_id:
        pref = settings().get(args.launch_id)
        if not pref:
            raise ValueError('该应用偏好不存在，请恢复或重新保存入口')
        gpu = next((g for g in detect_gpus() if g.key == pref['gpu']), None)
        if not gpu:
            raise ValueError('指定显卡不在线，请连接并授权拓展坞，或在显卡选择器里恢复默认启动')
        uris = args.command[1:] if args.command[:1] == ['--'] else args.command
        launch_desktop(pref['source'], gpu, pref['mode'], uris)
        return 0
    if args.gpu:
        gpu = next((g for g in detect_gpus() if g.key == args.gpu), None)
        if not gpu:
            raise ValueError('指定显卡不在线')
        if args.env:
            print(json.dumps(gpu_environment(gpu, args.mode), indent=2))
            return 0
        cmd = args.command[1:] if args.command[:1] == ['--'] else args.command
        if not cmd:
            raise ValueError('缺少启动命令')
        os.execvpe(cmd[0], cmd, child_environment(gpu, args.mode))
    return main_gui()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'GPU Selector: {error}', file=sys.stderr)
        # Desktop launches have no terminal; show the reason as a native dialog.
        if '--launch-id' in sys.argv:
            try:
                import gi
                gi.require_version('Gtk', '4.0')
                from gi.repository import Gtk, GLib
                Gtk.init()
                loop = GLib.MainLoop()
                dialog = Gtk.MessageDialog(text='无法启动应用', secondary_text=str(error), buttons=Gtk.ButtonsType.CLOSE)
                dialog.connect('response', lambda *_: loop.quit())
                dialog.present()
                loop.run()
            except Exception:
                pass
        sys.exit(1)
