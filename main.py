import argparse
import ctypes
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
if os.name == "nt":
    import winreg
    import winsound
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

# ============================================================
# Ajustes
# ============================================================

API_URL = "https://discord.com/api/v9/applications/detectable"
QUEST_API_URL = "https://discord.com/api/v9/quests/{}"

# 15 minutos + 15 s de margen por la latencia de Discord
DEFAULT_DURATION = 15 * 60 + 15
QUEST_MARGIN = 15

# Cuánto esperar a que Discord detecte el juego antes de avisar
DETECTION_TIMEOUT = 45
# Discord relee los appmanifest de Steam cada ~5 min: con el truco de Steam
# se relanza el juego hasta verlo detectado, como mucho este tiempo
STEAM_DETECTION_TIMEOUT = 8 * 60
STEAM_RETRY_WAIT = 20
STEAM_RETRY_PAUSE = 30

BASE_DIR = Path(os.environ.get("TEMP", "/tmp")) / "DiscordQuests"
MUTEX_NAME = r"Local\DiscordQuestCompleter"

# Caché de la API (dura 1 hora)
CACHE_FILE = BASE_DIR / "_cache_detectable.json"
CACHE_TTL = 3600

STEAM_REG = r"Software\Valve\Steam"

# Firma propia del exe falso: así una corrida futura reconoce un stub que dejó
# una corrida anterior muerta sin limpiar y no lo confunde con el juego real.
STUB_SIGNATURE = "DQC-STUB-SAM-7F3A9C"

# Lo que crea esta ejecución; cleanup() solo deshace esto
CREATED_DIRS = []
CREATED_FILES = []
STEAM_REG_UNDO = []       # (clave, valor, original | None, lo_que_escribimos)
STEAM_REG_NEW_KEYS = []

# Juegos sin EXE en la API de Discord (se detectan por SKU de Steam, el nombre da igual)
KNOWN_OVERRIDES = {
    "1445982809792708628": "AION2.exe",    # AION 2
    "1531874756096295054": "fc27.exe",     # EA Sports FC 27
    "451543892358201393": "aion.bin",      # Aion clásico
}

# UTF-8 en la consola de Windows
if sys.platform == "win32":
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console(legacy_windows=False)


# ============================================================
# Consola
# ============================================================

def set_console_title(title: str):
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


_CLOSE_HANDLER = None


def on_console_close(callback):
    """Python no ejecuta los finally si cierran la ventana con la X; esto sí."""
    global _CLOSE_HANDLER

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    def handler(event):
        if event in (2, 5, 6):  # CLOSE, LOGOFF, SHUTDOWN
            callback()
        return False

    _CLOSE_HANDLER = handler
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)


# ============================================================
# Mutex de Windows (evita dos instancias con el mismo juego)
# ============================================================

class SingleInstance:
    def __init__(self, name: str):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        return True

    def release(self):
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


# ============================================================
# Utilidades
# ============================================================

def format_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def safe_dir_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip(" .") or "Game"


def make_dirs(path: Path):
    """mkdir -p que anota la carpeta más alta que creó, para que cleanup() borre solo eso."""
    top = None
    for p in (path, *path.parents):
        if p.exists():
            break
        top = p
    path.mkdir(parents=True, exist_ok=True)
    if top:
        CREATED_DIRS.append(top)


def http_get_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "DiscordQuestCompleter/2.2"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Discord API: HTTP {e.code} en {url}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"No se pudo conectar con Discord: {e.reason}")
    except json.JSONDecodeError:
        raise RuntimeError("Discord devolvió un JSON inválido.")


# ============================================================
# Steam
# ============================================================

def get_steam_install_path() -> Path | None:
    reg_paths = [
        (winreg.HKEY_CURRENT_USER, STEAM_REG, "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ]
    for root, path, val_name in reg_paths:
        try:
            with winreg.OpenKey(root, path) as key:
                val, _ = winreg.QueryValueEx(key, val_name)
                if val and os.path.exists(val):
                    return Path(val)
        except OSError:
            pass

    default_paths = [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Steam",
    ]
    for p in default_paths:
        if p.exists():
            return p
    return None


def get_steam_sku(game: dict) -> str | None:
    for s in game.get("third_party_skus", []):
        if s.get("distributor") == "steam" and str(s.get("id", "")).isdigit():
            return str(s["id"])
    return None


def _reg_read(key: str, name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, name)
    except OSError:
        return None


def _reg_write(key: str, name: str, value, kind=4):  # 4 = REG_DWORD
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, name, 0, kind, value)


def fake_steam_launch(appid: str, game_name: str):
    """
    Escribe en el registro lo mismo que Steam cuando lanza un juego.
    El SteamObserver de Discord (discord_utils.node) cruza RunningAppID /
    Apps\\<id>\\Running con el installdir de appmanifest_<id>.acf para darle
    el SKU de Steam al proceso de steamapps/common/<installdir>/. Así detecta
    juegos sin ningún .exe en su base (FC 27, AION 2...). Hacen falta las dos
    cosas: solo el manifiesto (lo que hacía la versión original) no sirve.
    """
    app_key = rf"{STEAM_REG}\Apps\{appid}"
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, app_key))
    except OSError:
        STEAM_REG_NEW_KEYS.append(app_key)

    changes = [
        (app_key, "Installed", 1, winreg.REG_DWORD),
        (app_key, "Running", 1, winreg.REG_DWORD),
        (app_key, "Name", game_name, winreg.REG_SZ),
        (STEAM_REG, "RunningAppID", int(appid), winreg.REG_DWORD),
    ]
    for key, name, value, kind in changes:
        STEAM_REG_UNDO.append((key, name, _reg_read(key, name), value))
        _reg_write(key, name, value, kind)


def restore_steam_registry():
    for key, name, original, written in reversed(STEAM_REG_UNDO):
        if key in STEAM_REG_NEW_KEYS:
            continue
        current = _reg_read(key, name)
        if current is None or current[0] != written:
            continue  # Steam ya lo cambió (p. ej. lanzaste un juego real): no se pisa
        try:
            if original is None:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, name)
            else:
                _reg_write(key, name, original[0], original[1])
        except OSError:
            pass

    for key in STEAM_REG_NEW_KEYS:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass

    STEAM_REG_UNDO.clear()
    STEAM_REG_NEW_KEYS.clear()


# ============================================================
# Discord IPC (Rich Presence por named pipe) — solo con --ipc
# ============================================================

class DiscordIPC:
    """
    Se conecta a Discord por IPC y pone la actividad (Rich Presence).
    No hace avanzar misiones: los heartbeats salen solo de RunningGameStore.
    """

    OP_HANDSHAKE = 0
    OP_FRAME = 1
    OP_CLOSE = 2

    def __init__(self, client_id: str):
        self.client_id = client_id
        self._pipe = None
        self.connected = False

    def connect(self) -> bool:
        for i in range(10):
            pipe_path = rf"\\.\pipe\discord-ipc-{i}"
            try:
                if os.name != "nt":  # Linux: socket Unix en $XDG_RUNTIME_DIR
                    s = socket.socket(socket.AF_UNIX)
                    s.connect(f"{os.environ.get('XDG_RUNTIME_DIR', '/tmp')}/discord-ipc-{i}")
                    self._pipe = s.makefile("rwb", buffering=0)
                    break
                self._pipe = open(pipe_path, "r+b", 0)
                break
            except OSError:
                continue

        if self._pipe is None:
            return False

        try:
            self._send(self.OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
            op, data = self._recv()
            if data and data.get("evt") == "READY":
                self.connected = True
                return True
            self._close_pipe()
            return False
        except Exception:
            self._close_pipe()
            return False

    def set_activity(self, pid: int, game_name: str) -> bool:
        if not self.connected:
            return False
        try:
            payload = {
                "cmd": "SET_ACTIVITY",
                "args": {
                    "pid": pid,
                    "activity": {
                        "timestamps": {"start": int(time.time())},
                        "details": f"Playing {game_name}",
                    }
                },
                "nonce": str(time.time_ns())
            }
            self._send(self.OP_FRAME, payload)
            op, data = self._recv()
            return data is not None
        except Exception:
            return False

    def close(self):
        if self._pipe:
            try:
                self._send(self.OP_CLOSE, {})
            except Exception:
                pass
            self._close_pipe()
        self.connected = False

    def _send(self, opcode: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", opcode, len(raw))
        self._pipe.write(header + raw)
        self._pipe.flush()

    def _recv(self):
        header = self._read_exact(8)
        if header is None:
            return None, None
        opcode, length = struct.unpack("<II", header)
        body = self._read_exact(length)
        if body is None:
            return opcode, None
        try:
            return opcode, json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return opcode, None

    def _read_exact(self, n: int):
        data = b""
        while len(data) < n:
            chunk = self._pipe.read(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _close_pipe(self):
        if self._pipe:
            try:
                self._pipe.close()
            except Exception:
                pass
            self._pipe = None


# ============================================================
# API de Discord
# ============================================================

def get_detectable_games():
    """Lista de juegos detectables de Discord, cacheada 1 hora."""
    if CACHE_FILE.exists():
        try:
            if time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    result = json.load(f)
                if isinstance(result, list) and len(result) > 1000:
                    return result
        except Exception:
            pass

    result = http_get_json(API_URL)
    if not isinstance(result, list):
        raise RuntimeError("La API de Discord devolvió un formato inesperado.")

    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception:
        pass

    return result


def parse_quest_id(text: str) -> str | None:
    m = re.search(r"discord(?:app)?\.com/quests/(\d{17,20})", text) or re.fullmatch(r"\s*(\d{17,20})\s*", text)
    return m.group(1) if m else None


def get_quest_task(quest_id: str) -> tuple[dict, str, str, int]:
    """Devuelve (misión, tipo_de_tarea, application_id, segundos) de la tarea hacible en PC."""
    quest = http_get_json(QUEST_API_URL.format(quest_id))
    tasks = (quest.get("task_config_v2") or {}).get("tasks", {})
    for kind in ("PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP"):
        task = tasks.get(kind)
        if task and task.get("applications"):
            return quest, kind, str(task["applications"][0]["id"]), int(task.get("target", 900))
    raise RuntimeError(
        f"La misión {quest_id} no se puede hacer en PC con este método "
        f"(tareas: {', '.join(tasks) or 'ninguna'})."
    )


# ============================================================
# Búsqueda del juego
# ============================================================

def _score_match(name: str, query: str) -> int:
    """
    Prioridad (menor = mejor):
      0 — igual, 1 — empieza por la búsqueda, 2 — la contiene,
      3 — contiene todas las palabras en orden, -1 — no coincide
    """
    if name == query:
        return 0
    if name.startswith(query):
        return 1
    if query in name:
        return 2

    query_tokens = query.split()
    if len(query_tokens) >= 2:
        pos = 0
        for token in query_tokens:
            idx = name.find(token, pos)
            if idx < 0:
                return -1
            pos = idx + len(token)
        return 3

    return -1


def find_games(games, game_name: str):
    query = game_name.casefold().strip()

    scored = []
    for g in games:
        score = _score_match(str(g.get("name", "")).casefold(), query)

        if score < 0:
            for alias in g.get("aliases", []):
                alias_score = _score_match(str(alias).casefold(), query)
                if alias_score >= 0:
                    score = alias_score + 5
                    break

        if score >= 0:
            scored.append((score, g))

    scored.sort(key=lambda x: (x[0], len(str(x[1].get("name", "")))))
    return [g for _, g in scored]


def select_game(games, game_name: str):
    found = find_games(games, game_name)

    if not found:
        raise RuntimeError(
            f"«{game_name}» no está entre los juegos detectables de Discord.\n"
            f"  Prueba otra forma de escribirlo o solo una parte del nombre."
        )

    if len(found) == 1:
        return found[0]

    if str(found[0].get("name", "")).casefold() == game_name.casefold().strip():
        return found[0]

    console.print("\n[yellow]Hay varias coincidencias:[/yellow]\n")
    limit = min(len(found), 15)

    for index in range(limit):
        game = found[index]
        has_exe = "✓" if game.get("executables") else "✗"
        exe_color = "green" if game.get("executables") else "yellow"
        console.print(
            f"[cyan]\\[{index}][/cyan] {escape(game.get('name', 'Sin nombre'))} "
            f"[{exe_color}]\\[EXE: {has_exe}][/{exe_color}] "
            f"[dim](ID: {game.get('id', '?')})[/dim]"
        )

    if len(found) > limit:
        console.print(f"[dim]  ...y {len(found) - limit} más[/dim]")

    while True:
        choice = console.input("\nElige un número [0]: ").strip()
        if not choice:
            return found[0]
        try:
            index = int(choice)
            if 0 <= index < limit:
                return found[index]
        except ValueError:
            pass
        console.print("[red]Número no válido.[/red]")


# ============================================================
# Nombre del ejecutable
# ============================================================

def infer_franchise_exe(game_name: str, all_games: list) -> str | None:
    """
    Si el juego acaba en número/año (p. ej. 'EA Sports FC 27'), busca la
    entrega anterior (26, 25...) y adapta su patrón de nombre de EXE.
    """
    m = re.search(r"(\d+)\s*$", game_name)
    if not m:
        return None
    num_str = m.group(1)
    num = int(num_str)
    prefix = game_name[:m.start()].strip().lower()

    for prev_num in range(num - 1, max(0, num - 5), -1):
        prev_name = f"{prefix} {prev_num}"
        for g in all_games:
            if g.get("name", "").lower() == prev_name:
                execs = [
                    e.get("name") for e in g.get("executables", [])
                    if e.get("os") == "win32" and not e.get("is_launcher")
                ]
                if execs:
                    clean_execs = [e for e in execs if "trial" not in e.lower() and "showcase" not in e.lower()]
                    best = clean_execs[0] if clean_execs else execs[0]
                    return best.replace(str(prev_num), num_str)
    return None


def get_windows_executable(game: dict, all_games: list = None, custom_exe: str = None) -> tuple[str, str]:
    """
    Devuelve (nombre_exe, origen). Origen: 'custom' (a mano), 'api' (base de
    Discord), 'known' (tabla propia), 'franchise' (entrega anterior),
    'steam' / 'generated' (inventado a partir del nombre).
    """
    def norm(name: str) -> str:
        return name.replace("/", "\\").lstrip("\\")

    if custom_exe:
        return norm(custom_exe), "custom"

    game_id = str(game.get("id", ""))
    game_name = game.get("name", "Game")

    if game_id in KNOWN_OVERRIDES:
        return norm(KNOWN_OVERRIDES[game_id]), "known"

    GENERIC_NAMES = {"javaw.exe", "java.exe", "python.exe", "pythonw.exe", "node.exe"}
    valid_execs = []
    for e in game.get("executables", []):
        name = e.get("name", "")
        if e.get("os") != "win32" or not name or name.startswith(">"):
            continue
        if name.rsplit("/", 1)[-1].lower() in GENERIC_NAMES:
            continue
        valid_execs.append(e)

    if valid_execs:
        non_launcher = [e for e in valid_execs if not e.get("is_launcher")]
        chosen = non_launcher[0] if non_launcher else valid_execs[0]
        return norm(chosen["name"]), "api"

    if all_games:
        inferred = infer_franchise_exe(game_name, all_games)
        if inferred:
            return norm(inferred), "franchise"

    clean = re.sub(r"[^a-zA-Z0-9]", "", game_name) or "game"
    return f"{clean}.exe", "steam" if get_steam_sku(game) else "generated"


# ============================================================
# EXE falso
# ============================================================

def is_our_stub(path: Path) -> bool:
    """
    ¿Ese exe en la carpeta de Steam lo dejamos nosotros (stub de una corrida
    anterior que no limpió) o es el juego real? Sin esto, el script ve su propia
    basura, cree que es el juego instalado y se niega a usar el truco de Steam,
    con lo que Discord nunca detecta los juegos sin exe en su base (FC 27...).
    """
    sidecar = path.with_suffix(path.suffix + ".dqc")
    if sidecar.exists():
        return True
    try:
        if path.stat().st_size > 2_000_000:  # un juego real no pesa unos KB
            return False
        data = path.read_bytes()
    except OSError:
        return False
    return (STUB_SIGNATURE.encode("utf-16-le") in data
            or STUB_SIGNATURE.encode("utf-8") in data)


def prepare_dummy_executable(game: dict, executable_name: str) -> tuple[Path, bool]:
    """
    Crea el .exe falso. Si el juego está en Steam, lo pone en
    steamapps/common/<installdir>/, crea el appmanifest si no existe y
    simula en el registro que Steam lo lanzó. Devuelve (ruta_exe, usa_steam).
    """
    if os.name != "nt":  # Linux: Discord solo mira juegos bajo .../steamapps/common/ (o Wine)
        path = BASE_DIR / "steamapps" / "common" / safe_dir_name(game.get("name", "Game")) / executable_name.replace("\\", "/")
        make_dirs(path.parent)
        shutil.copy2(shutil.which("sleep"), path)  # binario real: nombre, ruta y /proc/<pid>/exe coinciden
        CREATED_FILES.append(path)
        return path, False
    steam_path = get_steam_install_path()
    steam_sku = get_steam_sku(game)
    game_name = game.get("name", "Game")
    full_exe_path = None
    use_steam = False

    if steam_path and steam_sku:
        steamapps = steam_path / "steamapps"
        manifest = steamapps / f"appmanifest_{steam_sku}.acf"
        install_dir = safe_dir_name(game_name)
        if manifest.exists():
            # Discord busca el exe en el installdir del manifiesto, no en el nombre del juego
            m = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text(encoding="utf-8", errors="replace"), re.I)
            if m:
                install_dir = m.group(1)

        candidate = steamapps / "common" / install_dir / executable_name
        if candidate.exists() and not is_our_stub(candidate):
            console.print(
                f"[yellow]⚠ {escape(str(candidate))} ya existe (¿juego real?). "
                f"No lo toco; uso %TEMP% y sin truco de Steam.[/yellow]"
            )
        else:
            full_exe_path = candidate
            use_steam = True
            if not manifest.exists():
                manifest.write_text(
                    f'"AppState"\n{{\n\t"appid"\t\t"{steam_sku}"\n\t"name"\t\t"{game_name.replace(chr(34), "")}"\n'
                    f'\t"installdir"\t\t"{install_dir}"\n\t"StateFlags"\t\t"4"\n}}\n',
                    encoding="utf-8"
                )
                CREATED_FILES.append(manifest)

    if full_exe_path is None:
        full_exe_path = BASE_DIR / str(game["id"]) / executable_name

    make_dirs(full_exe_path.parent)
    kill_exe(full_exe_path)

    _compile_dummy_cs(game, full_exe_path)
    CREATED_FILES.append(full_exe_path)

    if use_steam:
        # Marcador para que la próxima corrida reconozca este stub si no llego a limpiar
        marker = full_exe_path.with_suffix(full_exe_path.suffix + ".dqc")
        try:
            marker.write_text(STUB_SIGNATURE + "\n", encoding="utf-8")
            CREATED_FILES.append(marker)
        except OSError:
            pass
        fake_steam_launch(steam_sku, game_name)

    time.sleep(0.3)  # pausa para el antivirus
    return full_exe_path, use_steam


def kill_exe(exe_path: Path):
    """Mata solo procesos que corren exactamente este .exe, no cualquiera con el mismo nombre."""
    if os.name != "nt":  # en Linux el proceso lo para stop_process()
        return
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $env:DQC_EXE } "
         "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(os.environ, DQC_EXE=str(exe_path))
    )
    time.sleep(0.3)


def _compile_dummy_cs(game: dict, full_exe_path: Path):
    """Compila un .exe con ventana visible usando el csc.exe de .NET Framework."""
    windir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    csc = windir / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not csc.exists():
        csc = windir / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe"
    if not csc.exists():
        raise RuntimeError(
            "Hace falta csc.exe (.NET Framework 4.0+)."
        )

    safe_name = game.get("name", "Game").replace("\\", "\\\\").replace('"', "'")
    cs_code = f"""using System;
using System.Drawing;
using System.Windows.Forms;
class P {{
    [STAThread] static void Main() {{
        var f = new Form {{
            Text = "{safe_name}",
            Name = "{STUB_SIGNATURE}",
            Width = 460, Height = 200,
            FormBorderStyle = FormBorderStyle.FixedDialog,
            StartPosition = FormStartPosition.CenterScreen,
            MaximizeBox = false,
            TopMost = false
        }};
        var l = new Label {{
            Text = "{safe_name}\\n\\n" +
                   "Ventana para la misión de Discord.\\n" +
                   "¡No la cierres hasta que acabe el temporizador!",
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font("Segoe UI", 11)
        }};
        f.Controls.Add(l);
        Application.Run(f);
    }}
}}"""

    cs_file = full_exe_path.parent / "stub.cs"
    cs_file.write_text(cs_code, encoding="utf-8-sig")  # sin BOM csc lo lee en ANSI y salen tildes rotas

    result = subprocess.run(
        [str(csc), "/nologo", "/target:winexe", f"/out:{full_exe_path}", str(cs_file)],
        capture_output=True,
        text=True
    )
    cs_file.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"Error compilando el exe falso:\n{result.stdout}{result.stderr}")


# ============================================================
# Procesos
# ============================================================

def start_dummy(exe_path: Path):
    """Lanza el juego falso. Windows: ventana visible (hace falta para transmitirlo).
    Linux: copia de `sleep`, sin ventana (no vale para misiones de Stream)."""
    return subprocess.Popen(
        [str(exe_path)] + (["infinity"] if os.name != "nt" else []),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    if os.name != "nt":
        return process.kill()
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def cleanup():
    restore_steam_registry()

    for f in CREATED_FILES:
        if f.suffix.lower() == ".exe":
            kill_exe(f)  # también los que quedaron huérfanos de un reintento
    time.sleep(0.5)

    for f in CREATED_FILES:
        for _ in range(3):
            try:
                f.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.5)

    for d in CREATED_DIRS:
        shutil.rmtree(d, ignore_errors=True)

    CREATED_FILES.clear()
    CREATED_DIRS.clear()


# ============================================================
# Verificación: ¿Discord detectó el juego?
# ============================================================

def discord_log_offsets() -> dict:
    """Tamaño actual de renderer_js.log de cada Discord instalado (stable/PTB/Canary)."""
    appdata = Path(os.environ.get("APPDATA") or Path.home() / ".config")  # Linux: ~/.config/discord
    offsets = {}
    for name in ("discord", "discordptb", "discordcanary"):
        log = appdata / name / "logs" / "renderer_js.log"
        if log.exists():
            offsets[log] = log.stat().st_size
    return offsets


def wait_for_detection(offsets: dict, exe_path: Path, process, timeout: int) -> bool:
    """
    Busca la ruta del exe en lo nuevo de renderer_js.log: Discord la escribe
    (newPrimaryKey=...) cuando el proceso entra en su RunningGameStore, que es
    de donde salen los heartbeats de las misiones PLAY_ON_DESKTOP.
    Un "Running Games Changed" a secas no basta: también sale con visibleGame=null.
    """
    exe = str(exe_path).replace("\\", "/").lower()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        for log, start in offsets.items():
            try:
                with open(log, "rb") as f:
                    f.seek(start)
                    new = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            if exe in new.lower():
                return True
        time.sleep(1)
    return False


def launch_until_detected(exe_path: Path, retry: bool):
    """
    Lanza el juego falso y espera a verlo en el log de Discord. Con el truco
    de Steam lo relanza hasta que Discord relea los manifiestos (~5 min),
    porque clasifica el proceso al verlo nacer.
    Devuelve (proceso, detectado), detectado = None si no hay log de Discord.
    """
    deadline = time.monotonic() + (STEAM_DETECTION_TIMEOUT if retry else DETECTION_TIMEOUT)
    attempt = 1
    with console.status("Esperando a que Discord detecte el juego...") as status:
        while True:
            offsets = discord_log_offsets()
            process = start_dummy(exe_path)
            time.sleep(0.5)
            if process.poll() is not None:
                raise RuntimeError("El juego falso no arrancó.")
            if not offsets:
                return process, None

            wait = STEAM_RETRY_WAIT if retry else DETECTION_TIMEOUT
            if wait_for_detection(offsets, exe_path, process, wait):
                return process, True
            if not retry or time.monotonic() + STEAM_RETRY_PAUSE >= deadline:
                return process, False

            stop_process(process)
            attempt += 1
            status.update(
                f"Discord aún no relee Steam (lo hace cada ~5 min). Intento {attempt} en "
                f"{STEAM_RETRY_PAUSE} s; me rindo en {format_time(deadline - time.monotonic())}..."
            )
            time.sleep(STEAM_RETRY_PAUSE)


# ============================================================
# Interfaz
# ============================================================

def build_status_panel(game_name, executable_name, pid, progress, detected,
                       ipc_connected, exe_source, is_steam, active, remaining):
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold", width=16)
    table.add_column()

    table.add_row("Juego:", f"[bold cyan]{escape(game_name)}[/bold cyan]")

    source_labels = {
        "api": "base de Discord",
        "known": "tabla propia",
        "franchise": "entrega anterior",
        "steam": "inventado",
        "custom": "a mano",
        "generated": "inventado",
    }
    table.add_row("EXE:", f"{escape(executable_name)} [dim]({source_labels.get(exe_source, '')})[/dim]")
    table.add_row("PID:", str(pid))
    table.add_row("", "")

    if detected:
        table.add_row("Discord:", "[green]✓ detectó el juego[/green]")
    elif detected is None:
        table.add_row("Discord:", "[yellow]? no encontré su log; mira tu perfil[/yellow]")
    else:
        table.add_row("Discord:", "[red]✗ no vi que lo detectara: revisa tu perfil[/red]")
    if is_steam:
        table.add_row("", "[dim]truco de Steam activo (registro simulado)[/dim]")
    if ipc_connected:
        table.add_row("", f"[dim]Rich Presence por IPC: Jugando a {escape(game_name)}[/dim]")
    table.add_row("", "")

    table.add_row("", "[bold green]▶ Misión en curso[/bold green]" if active
                  else "[bold green]✓ ¡Misión completada![/bold green]")
    table.add_row("", "")
    table.add_row("", progress)
    table.add_row("", "")
    table.add_row("", f"Queda: [bold]{format_time(remaining)}[/bold]")

    return Panel(table, title="[bold bright_magenta]D-Q-C[/bold bright_magenta] [dim]·[/dim] [bold bright_cyan]By Eidanex[/bold bright_cyan]", border_style="bright_magenta", padding=(1, 2))


def run_timer(process, duration, **panel_args):
    progress = Progress(
        TextColumn(" "),
        BarColumn(bar_width=30),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%")
    )
    task = progress.add_task("quest", total=duration)
    start_time = time.monotonic()

    with Live(refresh_per_second=4, console=console) as live:
        while True:
            elapsed = time.monotonic() - start_time
            remaining = max(0, int(duration - elapsed))
            progress.update(task, completed=min(elapsed, duration))

            set_console_title(f"[{format_time(remaining)}] Discord Quest Completer — {panel_args['game_name']}")
            live.update(build_status_panel(
                pid=process.pid, progress=progress, active=remaining > 0, remaining=remaining, **panel_args
            ))

            if remaining <= 0:
                break

            if process.poll() is not None:
                raise RuntimeError(
                    "Cerraste la ventana del juego antes de tiempo.\n"
                    "Vuelve a lanzarlo y no la cierres hasta que acabe."
                )

            time.sleep(0.25)

    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def show_error(message: str):
    console.print()
    console.print(Panel(Text(f"✗ {message}", style="red"), title="Error", border_style="red"))


def show_banner():
    banner = Text()
    banner.append("D-Q-C", style="bold bright_magenta")
    banner.append("  ·  ", style="dim")
    banner.append("By Eidanex", style="bold bright_cyan")
    banner.append("   v2.2.0", style="dim green")

    console.print()
    console.print(
        Panel(
            banner,
            subtitle="[dim]Misiones de Discord sin tocar el cliente ni tu token[/dim]",
            border_style="bright_magenta",
            padding=(0, 2)
        )
    )
    console.print()


# ============================================================
# Entrada
# ============================================================

def main():
    set_console_title("Discord Quest Completer")
    show_banner()

    parser = argparse.ArgumentParser(description="Discord Quest Completer")
    parser.add_argument("game", nargs="?", help="Nombre del juego, o ID/enlace de la misión (discord.com/quests/...)")
    parser.add_argument("--game", dest="game_option", help="Nombre del juego, o ID/enlace de la misión")
    parser.add_argument("--exe", dest="exe_override", help="Forzar el nombre del exe (p. ej. AION2.exe)")
    parser.add_argument(
        "--duration",
        type=int,
        help=f"Duración en segundos (por defecto: la de la misión + {QUEST_MARGIN} s, o {DEFAULT_DURATION})"
    )
    parser.add_argument("--list", action="store_true", help="Listar juegos detectables")
    parser.add_argument("--ipc", action="store_true", help="Además, poner Rich Presence por IPC (no hace avanzar la misión)")

    args = parser.parse_args()

    if args.list:
        console.print("[cyan]Cargando la base de juegos...[/cyan]")
        games = get_detectable_games()
        with_exe = [g for g in games if g.get("executables")]
        console.print(f"\n[green]Juegos con EXE de Windows: {len(with_exe)}[/green]")
        console.print(f"[yellow]Sin EXE (solo por SKU/serie): {len(games) - len(with_exe)}[/yellow]\n")
        for g in with_exe[:50]:
            console.print(f"  • {escape(g.get('name', ''))}")
        if len(with_exe) > 50:
            console.print(f"  [dim]...y {len(with_exe) - 50} más[/dim]")
        return 0

    query = args.game_option or args.game
    if not query:
        query = console.input(
            "[bold]Juego o enlace de la misión[/bold] (p. ej. Marvel Rivals): "
        ).strip()

    if not query:
        show_error("No pusiste ningún juego.")
        return 1

    if args.duration is not None and args.duration <= 0:
        show_error("La duración tiene que ser mayor que 0 segundos.")
        return 1

    instance = SingleInstance(MUTEX_NAME + "_" + re.sub(r"\W", "", query.casefold())[-60:])
    if not instance.acquire():
        console.print()
        console.print(Panel(
            "[yellow]Ya hay una sesión corriendo con este juego.[/yellow]\n\nEspera a que termine.",
            title="Ya está corriendo",
            border_style="yellow"
        ))
        return 1

    process = None
    ipc = None
    if os.name == "nt":
        on_console_close(cleanup)

    try:
        console.print("[cyan]Cargando la base de juegos de Discord...[/cyan]")
        games = get_detectable_games()
        console.print(f"[dim]{len(games)} juegos cargados[/dim]\n")

        duration = args.duration
        quest_id = parse_quest_id(query)
        if quest_id:
            quest, kind, app_id, target = get_quest_task(quest_id)
            selected_game = next((g for g in games if str(g.get("id")) == app_id), None)
            if selected_game is None:
                raise RuntimeError(f"La app {app_id} de la misión no está en la base de juegos detectables de Discord.")
            quest_name = quest.get("messages", {}).get("quest_name", quest_id)
            console.print(
                f"[green]✓[/green] Misión: [bold]{escape(quest_name)}[/bold] — {kind}, "
                f"{format_time(target)}, caduca {quest.get('expires_at', '?')[:10]}"
            )
            console.print("[dim]  (acéptala en Discord antes, si no, no cuenta)[/dim]")
            if kind == "STREAM_ON_DESKTOP":
                console.print("[yellow]  Es de transmitir: entra a un canal de voz con alguien más y comparte la ventana del juego.[/yellow]")
            if duration is None:
                duration = target + QUEST_MARGIN
        else:
            selected_game = select_game(games, query)
        duration = duration or DEFAULT_DURATION

        selected_name = selected_game.get("name", query)
        game_id = str(selected_game.get("id", ""))

        executable_name, exe_source = get_windows_executable(
            selected_game, all_games=games, custom_exe=args.exe_override
        )

        console.print(f"[green]✓[/green] Juego: [bold]{escape(selected_name)}[/bold]")
        console.print(f"[green]✓[/green] Exe: [bold]{escape(executable_name)}[/bold] [dim]({exe_source})[/dim]")
        console.print(f"[green]✓[/green] Duración: [bold]{format_time(duration)}[/bold]\n")

        full_exe_path, is_steam = prepare_dummy_executable(selected_game, executable_name)
        if not is_steam and exe_source != "api":
            console.print(
                "[yellow]⚠ Este juego no tiene exe en la base de Discord ni SKU de Steam usable: "
                "lo más probable es que Discord no lo detecte.[/yellow]"
            )

        process, detected = launch_until_detected(full_exe_path, retry=is_steam)
        if detected:
            console.print("[green]✓ Discord detectó el juego. El tiempo empieza a contar ahora.[/green]\n")
        elif detected is False:
            console.print(
                "[red]✗ No vi a Discord detectarlo: lo más seguro es que la misión NO avance.[/red]\n"
                "[dim]  ¿Discord abierto? ¿Detección de juegos activada? Si tienes otro juego abierto,\n"
                "  Discord puede no escribir esta ruta en el log: mira si tu perfil dice «Jugando a ...».[/dim]\n"
            )

        ipc_connected = False
        if args.ipc and game_id:
            try:
                ipc = DiscordIPC(client_id=game_id)
                ipc_connected = ipc.connect() and ipc.set_activity(pid=process.pid, game_name=selected_name)
            except Exception:
                ipc_connected = False

        run_timer(
            process=process,
            duration=duration,
            game_name=selected_name,
            executable_name=executable_name,
            detected=detected,
            ipc_connected=ipc_connected,
            exe_source=exe_source,
            is_steam=is_steam,
        )

        console.print()
        console.print(Panel(
            "[bold green]✓ ¡Tiempo cumplido![/bold green]\n\n"
            "Abre Discord → Misiones y reclama la recompensa.",
            title="Listo",
            border_style="green",
            padding=(1, 2)
        ))
        return 0

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelado.[/yellow]")
        return 130
    except Exception as e:
        show_error(str(e))
        return 1
    finally:
        if ipc:
            ipc.close()
        stop_process(process)
        cleanup()
        instance.release()
        set_console_title("Discord Quest Completer")


if __name__ == "__main__":
    sys.exit(main())
