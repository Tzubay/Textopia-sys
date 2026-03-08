#!/usr/bin/env python3
import argparse
import base64
import curses
import json
import os
import queue
import re
import shlex
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAX_LINE = 8192
CHUNK_SIZE = 64 * 1024
WIRE_CHUNK_SIZE = 3 * 1024
MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # debe coincidir con servidor
AUTO_WHO_INTERVAL = 15  # 0 para desactivar


def encode_line(text: str) -> bytes:
    return (text.rstrip("\r\n") + "\n").encode("utf-8", errors="replace")


def recv_line(sock: socket.socket, buffer: bytearray) -> Optional[str]:
    while True:
        nl = buffer.find(b"\n")
        if nl != -1:
            line = buffer[:nl]
            del buffer[: nl + 1]
            if len(line) > MAX_LINE:
                line = line[:MAX_LINE]
            return line.decode("utf-8", errors="replace").rstrip("\r")

        chunk = sock.recv(4096)
        if not chunk:
            return None
        buffer.extend(chunk)
        if len(buffer) > MAX_LINE * 2:
            buffer.clear()
            return "* (cliente) Buffer demasiado grande; se limpió por seguridad."


def read_exact(sock: socket.socket, buffer: bytearray, n: int) -> bytes:
    out = bytearray()
    if n <= 0:
        return b""

    if buffer:
        take = min(len(buffer), n)
        out += buffer[:take]
        del buffer[:take]
        n -= take

    while n > 0:
        chunk = sock.recv(min(65536, n))
        if not chunk:
            raise ConnectionError("Socket cerrado durante transferencia")
        out += chunk
        n -= len(chunk)
    return bytes(out)


@dataclass
class Message:
    ts: str
    text: str


@dataclass
class Conversation:
    key: str
    title: str
    kind: str  # GLOBAL | DM | ROOM | SYSTEM
    messages: List[Message] = field(default_factory=list)
    unread: int = 0


@dataclass
class TransferState:
    transfer_id: str
    direction: str  # UP | DOWN
    filename: str
    scope: str
    total: int
    done: int
    started_at: float
    finished: bool = False
    error: str = ""
    out_path: str = ""
    file_obj: Optional[object] = None


class ChatClientTUI:
    def __init__(self, host: str, port: int, nick: str):
        self.host = host
        self.port = port
        self.nick = nick

        self.sock: Optional[socket.socket] = None
        self.stop_event = threading.Event()

        self.state_lock = threading.RLock()
        self.send_lock = threading.Lock()

        self.conversations: Dict[str, Conversation] = {}
        self.order: List[str] = []
        self.selected_index = 0

        self.input_buffer = ""
        self.status_line = f"Conectando a {host}:{port}..."
        self.toast_text = ""
        self.toast_until = 0.0

        self.online_users = set()
        self.public_rooms = set()
        self.private_rooms = set()

        # descargas pendientes: (scope, filename) -> dest_path
        self.pending_downloads: Dict[Tuple[str, str], str] = {}
        self.transfers: Dict[str, TransferState] = {}

        # scroll por conversación
        self.scroll_offsets: Dict[str, int] = {}

        self._ensure_conversation("GLOBAL", "Global", "GLOBAL")
        self._ensure_conversation("SYSTEM", "Sistema", "SYSTEM")

        self.last_who = 0.0

    # -------------------------
    # Estado
    # -------------------------
    def _now(self) -> str:
        return time.strftime("%H:%M:%S")

    def _ensure_conversation(self, key: str, title: str, kind: str) -> Conversation:
        with self.state_lock:
            if key not in self.conversations:
                self.conversations[key] = Conversation(key=key, title=title, kind=kind)
                self.order.append(key)
                self.scroll_offsets[key] = 0
            return self.conversations[key]

    def _selected_key(self) -> str:
        with self.state_lock:
            if not self.order:
                return "GLOBAL"
            self.selected_index = max(0, min(self.selected_index, len(self.order) - 1))
            return self.order[self.selected_index]

    def _selected_conv(self) -> Conversation:
        return self.conversations[self._selected_key()]

    def _append_message(self, conv_key: str, text: str, title: Optional[str] = None, kind: Optional[str] = None):
        with self.state_lock:
            if conv_key not in self.conversations:
                self._ensure_conversation(conv_key, title or conv_key, kind or "SYSTEM")
            conv = self.conversations[conv_key]
            conv.messages.append(Message(self._now(), text))

            if conv_key != self._selected_key():
                conv.unread += 1
                self._toast(f"Nuevo mensaje en {conv.title}")

            if len(conv.messages) > 1200:
                conv.messages = conv.messages[-1200:]

    def _toast(self, text: str, seconds: float = 3.0):
        with self.state_lock:
            self.toast_text = text
            self.toast_until = time.time() + seconds

    def _clear_toast_if_expired(self):
        with self.state_lock:
            if self.toast_text and time.time() > self.toast_until:
                self.toast_text = ""
                self.toast_until = 0.0

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        if seconds < 0 or seconds > 24 * 3600:
            return "--:--"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_rate(bytes_per_sec: float) -> str:
        if bytes_per_sec <= 0:
            return "0 B/s"
        units = ["B/s", "KB/s", "MB/s", "GB/s"]
        v = bytes_per_sec
        for u in units:
            if v < 1024 or u == units[-1]:
                if u == "B/s":
                    return f"{int(v)} {u}"
                return f"{v:.1f} {u}"
            v /= 1024.0
        return f"{int(bytes_per_sec)} B/s"

    def _build_transfer_status(self) -> Optional[str]:
        with self.state_lock:
            active = [t for t in self.transfers.values() if not t.finished and not t.error]
            if not active:
                return None
            t = max(active, key=lambda x: x.started_at)
            elapsed = max(0.001, time.time() - t.started_at)
            rate = t.done / elapsed
            pct = int((t.done / t.total) * 100) if t.total > 0 else 0
            width = 18
            fill = int(width * min(1.0, (t.done / t.total) if t.total > 0 else 0.0))
            bar = "#" * fill + "-" * (width - fill)
            remain = max(0, t.total - t.done)
            eta = self._fmt_eta(remain / rate if rate > 0 else -1)
            tag = "UP" if t.direction == "UP" else "DOWN"
            return f"{tag} {t.filename} [{bar}] {pct}% {self._fmt_rate(rate)} ETA {eta}"

    def _select_next(self):
        with self.state_lock:
            if self.order:
                self.selected_index = (self.selected_index + 1) % len(self.order)
                self.conversations[self.order[self.selected_index]].unread = 0

    def _select_prev(self):
        with self.state_lock:
            if self.order:
                self.selected_index = (self.selected_index - 1) % len(self.order)
                self.conversations[self.order[self.selected_index]].unread = 0

    def _select_conv_key(self, key: str) -> None:
        with self.state_lock:
            if key in self.order:
                self.selected_index = self.order.index(key)
                self.conversations[key].unread = 0

    # -------------------------
    # Red
    # -------------------------
    def connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self.host, self.port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self.send_raw(f"NICK {self.nick}")
        self.status_line = f"Conectado a {self.host}:{self.port} como {self.nick}"

    def send_raw(self, text: str):
        if not self.sock:
            return
        payload = encode_line(text)
        try:
            with self.send_lock:
                self.sock.sendall(payload)
        except OSError:
            self.status_line = "Error enviando."
            self.stop_event.set()

    def close(self):
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -------------------------
    # Parseo de mensajes
    # -------------------------
    def _scope_to_conv(self, scope: str) -> str:
        # scope esperado: ROOM:Amigos, DM:Juan
        if scope.startswith("ROOM:"):
            room = scope.split(":", 1)[1]
            return f"ROOM:{room}"
        if scope.startswith("DM:"):
            peer = scope.split(":", 1)[1]
            return f"DM:{peer}"
        return "SYSTEM"

    def process_server_line(self, line: str, buffer: bytearray):
        line = line.rstrip()

        # --- Protocolos de archivos (respuestas del server) ---
        if line.startswith("FILELISTBEGIN "):
            try:
                meta = json.loads(line[len("FILELISTBEGIN "):].strip())
                scope = str(meta.get("scope", "SYSTEM"))
                count = int(meta.get("count", 0))
            except Exception:
                return
            conv_key = self._scope_to_conv(scope)
            # asegúrate de que exista
            if conv_key.startswith("ROOM:"):
                title = conv_key.split(":", 1)[1]
                self._ensure_conversation(conv_key, title, "ROOM")
            elif conv_key.startswith("DM:"):
                title = conv_key.split(":", 1)[1]
                self._ensure_conversation(conv_key, title, "DM")
            self._append_message(conv_key, f"* Archivos disponibles ({count}):")
            return

        if line.startswith("FILEITEM "):
            try:
                meta = json.loads(line[len("FILEITEM "):].strip())
                scope = str(meta.get("scope", "SYSTEM"))
                fn = str(meta.get("filename", ""))
                size = meta.get("size", 0)
                frm = str(meta.get("from", "?"))
                ts = str(meta.get("ts", ""))
            except Exception:
                return
            conv_key = self._scope_to_conv(scope)
            self._append_message(conv_key, f"- {fn}  ({size} bytes)  from={frm}  ts={ts}")
            return

        if line.startswith("FILELISTEND "):
            try:
                meta = json.loads(line[len("FILELISTEND "):].strip())
                scope = str(meta.get("scope", "SYSTEM"))
            except Exception:
                return
            conv_key = self._scope_to_conv(scope)
            self._append_message(conv_key, "* Fin de lista.")
            return

        if line.startswith("FILEERR "):
            try:
                meta = json.loads(line[len("FILEERR "):].strip())
                scope = str(meta.get("scope", "SYSTEM"))
                err = str(meta.get("error", "Error"))
                transfer_id = str(meta.get("id", ""))
            except Exception:
                return
            conv_key = self._scope_to_conv(scope)
            self._append_message(conv_key, f"* Error: {err}")
            if transfer_id:
                self._finish_transfer(transfer_id, err)
            self._toast(err)
            return

        if line.startswith("FILEDOWNLOADBEGIN "):
            try:
                meta = json.loads(line[len("FILEDOWNLOADBEGIN "):].strip())
                transfer_id = str(meta.get("id", ""))
                scope = str(meta.get("scope", "SYSTEM"))
                filename = str(meta.get("filename", "file"))
                size = int(meta.get("size", 0))
            except Exception:
                return

            if not transfer_id:
                return
            conv_key = self._scope_to_conv(scope)
            try:
                key = (scope, filename)
                dest = self.pending_downloads.pop(key, "")
                out_path = self._resolve_download_path(filename, dest)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                fp = out_path.open("wb")
            except Exception:
                self._append_message(conv_key, "* Error preparando descarga.")
                self._toast("Error descargando")
                return

            with self.state_lock:
                self.transfers[transfer_id] = TransferState(
                    transfer_id=transfer_id,
                    direction="DOWN",
                    filename=filename,
                    scope=scope,
                    total=max(0, size),
                    done=0,
                    started_at=time.time(),
                    out_path=str(out_path),
                    file_obj=fp,
                )
            self.status_line = f"Descargando {filename}..."
            return

        if line.startswith("FILEDOWNLOADCHUNK "):
            try:
                meta = json.loads(line[len("FILEDOWNLOADCHUNK "):].strip())
                transfer_id = str(meta.get("id", ""))
                data_b64 = str(meta.get("data", ""))
            except Exception:
                return
            if not transfer_id or not data_b64:
                return
            with self.state_lock:
                t = self.transfers.get(transfer_id)
            if not t or t.direction != "DOWN" or t.finished:
                return
            try:
                chunk = base64.b64decode(data_b64.encode("ascii"), validate=True)
                if t.file_obj:
                    t.file_obj.write(chunk)
                with self.state_lock:
                    t.done += len(chunk)
            except Exception:
                self._finish_transfer(transfer_id, "Error procesando chunk")
            return

        if line.startswith("FILEDOWNLOADEND "):
            try:
                meta = json.loads(line[len("FILEDOWNLOADEND "):].strip())
                transfer_id = str(meta.get("id", ""))
            except Exception:
                return
            t = self._finish_transfer(transfer_id)
            if not t:
                return
            conv_key = self._scope_to_conv(t.scope)
            if t.error:
                self._append_message(conv_key, f"* Error de descarga: {t.error}")
                self._toast("Error descargando")
            else:
                self._append_message(conv_key, f"* Archivo descargado: {t.out_path}")
                self._toast("Descarga completa")
            return

        # --- 0) Líneas de /who: SOLO actualizan estado, NO se guardan como mensajes ---
        m = re.match(r"^\*\s+Conectados:\s+(.*)$", line)
        if m:
            raw = m.group(1).strip()
            self.online_users.clear()
            if raw != "(nadie)":
                for user in [x.strip() for x in raw.split(",") if x.strip()]:
                    self.online_users.add(user)
            return

        m = re.match(r"^\*\s+Salas PUBLIC:\s+(.*)$", line)
        if m:
            raw = m.group(1).strip()
            self.public_rooms.clear()
            if raw != "(ninguna)":
                for room in [x.strip() for x in raw.split(",") if x.strip()]:
                    self.public_rooms.add(room)
                    self._ensure_conversation(f"ROOM:{room}", room, "ROOM")
            return

        m = re.match(r"^\*\s+Tus salas PRIVATE \(solo si eres miembro\):\s+(.*)$", line)
        if m:
            raw = m.group(1).strip()
            self.private_rooms.clear()
            if raw != "(ninguna)":
                for room in [x.strip() for x in raw.split(",") if x.strip()]:
                    self.private_rooms.add(room)
                    self._ensure_conversation(f"ROOM:{room}", room, "ROOM")
            return

        m = re.match(r"^\*\s+Sala activa:\s+(.*)$", line)
        if m:
            return

        # --- 1) Mensaje de sala ---
        m = re.match(r"^\[Room:([^\]]+)\]\s+([^:]+):\s+(.*)$", line)
        if m:
            room, sender, msg = m.groups()
            key = f"ROOM:{room}"
            self._ensure_conversation(key, room, "ROOM")
            self._append_message(key, f"{sender}: {msg}")
            return

        # --- 2) DM entrante ---
        m = re.match(r"^\[DM de ([^\]]+)\]\s+(.*)$", line)
        if m:
            user, msg = m.groups()
            key = f"DM:{user}"
            self._ensure_conversation(key, user, "DM")
            self.online_users.add(user)
            self._append_message(key, f"{user}: {msg}")
            return

        # --- 3) DM saliente (eco) ---
        m = re.match(r"^\[DM a ([^\]]+)\]\s+(.*)$", line)
        if m:
            user, msg = m.groups()
            key = f"DM:{user}"
            self._ensure_conversation(key, user, "DM")
            self._append_message(key, f"Tú: {msg}")
            return

        # --- 4) Global [user] msg ---
        m = re.match(r"^\[([^\]]+)\]\s+(.*)$", line)
        if m:
            sender, msg = m.groups()
            self.online_users.add(sender)
            self._append_message("GLOBAL", f"{sender}: {msg}")
            return

        # --- 5) Eventos de join/leave los ponemos en GLOBAL ---
        m = re.match(r"^\*\s+([A-Za-z0-9_\-]+)\s+se unió al chat\.$", line)
        if m:
            user = m.group(1)
            self.online_users.add(user)
            self._append_message("GLOBAL", line)
            return

        m = re.match(r"^\*\s+([A-Za-z0-9_\-]+)\s+(salió|desconectado|fue desconectado.*)\.?$", line)
        if m:
            user = m.group(1)
            self.online_users.discard(user)
            self._append_message("GLOBAL", line)
            return

        # Invitación a sala
        m = re.match(r"^\*\s+Te agregaron a la sala '([^']+)'\s+\((PUBLIC|PRIVATE)\)\s+por\s+([A-Za-z0-9_\-]+)\.", line)
        if m:
            room, status, owner = m.groups()
            key = f"ROOM:{room}"
            self._ensure_conversation(key, room, "ROOM")
            if status == "PUBLIC":
                self.public_rooms.add(room)
            else:
                self.private_rooms.add(room)
            self._toast(f"Te añadieron a {room} ({status})")
            self._append_message(key, f"* Fuiste agregado por {owner} al grupo {room} ({status})")
            return

        # Ya eres miembro de...
        m = re.match(r"^\*\s+Ya eres miembro de:\s+(.*)$", line)
        if m:
            rooms = [r.strip() for r in m.group(1).split(",") if r.strip()]
            for room in rooms:
                self._ensure_conversation(f"ROOM:{room}", room, "ROOM")
            return

        # fallback
        self._append_message("SYSTEM", line)

    def receiver_loop(self):
        assert self.sock is not None
        buf = bytearray()
        try:
            while not self.stop_event.is_set():
                line = recv_line(self.sock, buf)
                if line is None:
                    self.status_line = "Conexión cerrada por el servidor."
                    self.stop_event.set()
                    break
                self.process_server_line(line, buf)
        except OSError:
            self.status_line = "Conexión interrumpida."
            self.stop_event.set()

    def auto_who_loop(self):
        if AUTO_WHO_INTERVAL <= 0:
            return
        while not self.stop_event.is_set():
            now = time.time()
            if now - self.last_who >= AUTO_WHO_INTERVAL:
                self.send_raw("/who")
                self.last_who = now
            time.sleep(0.5)

    # -------------------------
    # Archivos: comandos cliente
    # -------------------------
    @staticmethod
    def _parse_bracket_list(s: str) -> List[str]:
        s = s.strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1].strip()
        if not s:
            return []
        parts = [p.strip() for p in s.split(",")]
        return [p for p in parts if p]

    def _resolve_download_path(self, filename: str, dest: str) -> Path:
        safe = os.path.basename(filename) or "file"

        # Si no se especificó destino, usar ./DownloadsLocal/
        if not dest:
            base = Path.cwd() / "DownloadsLocal"
            base.mkdir(parents=True, exist_ok=True)
            out_path = base / safe
        else:
            p = Path(dest).expanduser()
            dest_str = dest.strip()
            looks_like_dir = dest_str.endswith("/") or dest_str.endswith("\\")

            if p.exists() and p.is_dir():
                looks_like_dir = True

            if looks_like_dir:
                p.mkdir(parents=True, exist_ok=True)
                out_path = p / safe
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                out_path = p

        # evitar overwrite
        if out_path.exists():
            stem, ext = os.path.splitext(out_path.name)
            k = 1
            while True:
                cand = out_path.with_name(f"{stem}({k}){ext}")
                if not cand.exists():
                    out_path = cand
                    break
                k += 1

        return out_path

    def _finish_transfer(self, transfer_id: str, error: str = "") -> Optional[TransferState]:
        with self.state_lock:
            t = self.transfers.get(transfer_id)
            if not t:
                return None
            t.finished = True
            if error:
                t.error = error
        if t.file_obj:
            try:
                t.file_obj.close()
            except Exception:
                pass
            t.file_obj = None
        return t

    def start_upload(self, targets: List[str], filepath: str):
        if not self.sock:
            self._toast("No conectado")
            return

        def worker():
            transfer_id = ""
            try:
                path = Path(filepath).expanduser()
                if not path.exists() or not path.is_file():
                    self._toast("Archivo no existe")
                    self.status_line = "Archivo no existe"
                    return

                size = path.stat().st_size
                if size > MAX_UPLOAD_SIZE:
                    self._toast("Archivo muy grande")
                    self.status_line = f"Máximo: {MAX_UPLOAD_SIZE} bytes"
                    return

                filename = path.name
                transfer_id = f"up-{int(time.time() * 1000)}-{threading.get_ident()}"
                meta = {"id": transfer_id, "targets": targets, "filename": filename, "size": size}
                header = "FILEUPLOADBEGIN " + json.dumps(meta, ensure_ascii=False)

                sent = 0
                self.status_line = f"Subiendo {filename}..."
                with self.state_lock:
                    self.transfers[transfer_id] = TransferState(
                        transfer_id=transfer_id,
                        direction="UP",
                        filename=filename,
                        scope="MULTI",
                        total=size,
                        done=0,
                        started_at=time.time(),
                    )

                self.send_raw(header)
                with path.open("rb") as f:
                    while True:
                        chunk = f.read(WIRE_CHUNK_SIZE)
                        if not chunk:
                            break
                        payload = {
                            "id": transfer_id,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        }
                        self.send_raw("FILEUPLOADCHUNK " + json.dumps(payload, ensure_ascii=False))
                        sent += len(chunk)
                        with self.state_lock:
                            t = self.transfers.get(transfer_id)
                            if t:
                                t.done = sent

                self.send_raw("FILEUPLOADEND " + json.dumps({"id": transfer_id}, ensure_ascii=False))
                self._finish_transfer(transfer_id)

                self.status_line = f"Upload completo: {filename}"
                self._toast("Upload completo")

            except Exception:
                if transfer_id:
                    self._finish_transfer(transfer_id, "Error en upload")
                self.status_line = "Error en upload"
                self._toast("Error en upload")

        threading.Thread(target=worker, daemon=True).start()

    def request_viewfiles(self, limit: int):
        conv = self._selected_conv()
        if conv.kind == "ROOM":
            meta = {"kind": "ROOM", "room": conv.title, "limit": limit}
            self.send_raw("FILEVIEW " + json.dumps(meta, ensure_ascii=False))
            return
        if conv.kind == "DM":
            meta = {"kind": "DM", "peer": conv.title, "limit": limit}
            self.send_raw("FILEVIEW " + json.dumps(meta, ensure_ascii=False))
            return
        self._toast("Ve a un DM o Room")

    def request_download(self, filename: str, dest: str):
        conv = self._selected_conv()
        if conv.kind == "ROOM":
            scope = f"ROOM:{conv.title}"
            self.pending_downloads[(scope, filename)] = dest
            meta = {"kind": "ROOM", "room": conv.title, "filename": filename}
            self.send_raw("FILEDOWNLOAD " + json.dumps(meta, ensure_ascii=False))
            self.status_line = f"Descargando {filename}..."
            return
        if conv.kind == "DM":
            scope = f"DM:{conv.title}"
            self.pending_downloads[(scope, filename)] = dest
            meta = {"kind": "DM", "peer": conv.title, "filename": filename}
            self.send_raw("FILEDOWNLOAD " + json.dumps(meta, ensure_ascii=False))
            self.status_line = f"Descargando {filename}..."
            return
        self._toast("Ve a un DM o Room")

    # -------------------------
    # Envío según conversación
    # -------------------------
    def send_from_current_conversation(self, text: str):
        text = text.strip()
        if not text:
            return

        # Comandos locales
        if text.lower() in ("/quit", "/exit"):
            self.stop_event.set()
            return
        if text.lower() == "/clear":
            self.input_buffer = ""
            return

        # Parseo de comandos de archivos (cliente)
        if text.startswith("/"):
            # usamos shlex para rutas con espacios
            try:
                parts = shlex.split(text, posix=(os.name != "nt"))
            except ValueError:
                self._toast("Comando inválido")
                return

            cmd = parts[0].lower()

            # comandos locales para abrir conversaciones (no manda nada al servidor)
            if cmd == "/open":
                if len(parts) < 2:
                    self._toast("Uso: /open nick")
                    return
                peer = parts[1].strip()
                key = f"DM:{peer}"
                self._ensure_conversation(key, peer, "DM")
                self._select_conv_key(key)
                self.status_line = f"Abierto DM con {peer}"
                return

            if cmd == "/openroom":
                if len(parts) < 2:
                    self._toast("Uso: /openroom Sala")
                    return
                room = parts[1].strip()
                key = f"ROOM:{room}"
                self._ensure_conversation(key, room, "ROOM")
                self._select_conv_key(key)
                self.status_line = f"Abierta sala {room}"
                return

            if cmd == "/upload":
                if len(parts) < 3:
                    self._toast("Uso: /upload [@user,Room] /ruta/archivo")
                    return
                targets = self._parse_bracket_list(parts[1])
                filepath = " ".join(parts[2:]).strip()
                if not targets or not filepath:
                    self._toast("Upload: faltan datos")
                    return
                self.start_upload(targets, filepath)
                return

            if cmd == "/download":
                if len(parts) < 3:
                    self._toast("Uso: /download archivo destino")
                    return
                filename = parts[1]
                dest = " ".join(parts[2:]).strip()
                self.request_download(filename, dest)
                return

            if cmd == "/viewfiles":
                limit = 0
                if len(parts) >= 2:
                    try:
                        limit = max(0, int(parts[1]))
                    except ValueError:
                        limit = 0
                self.request_viewfiles(limit)
                return

            # Comandos normales al servidor
            self.send_raw(text)
            return

        current = self._selected_conv()

        if current.kind == "GLOBAL":
            self.send_raw(text)
            self._append_message("GLOBAL", f"Tú: {text}")
            return

        if current.kind == "DM":
            self.send_raw(f"@{current.title} {text}")
            return

        if current.kind == "ROOM":
            self.send_raw(f"@{current.title} {text}")
            return

        self.send_raw(text)

    # -------------------------
    # UI (curses)
    # -------------------------
    @staticmethod
    def wrap_text(text: str, width: int) -> List[str]:
        if width <= 1:
            return [text]
        out = []
        while len(text) > width:
            cut = text.rfind(" ", 0, width)
            if cut == -1:
                cut = width
            out.append(text[:cut])
            text = text[cut:].lstrip()
        out.append(text)
        return out

    def draw(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        min_h = 16
        min_w = 80
        if h < min_h or w < min_w:
            msg = f"Terminal demasiado pequeña. Mínimo {min_w}x{min_h}"
            stdscr.addstr(0, 0, msg[: max(1, w - 1)])
            stdscr.refresh()
            return

        sidebar_w = max(30, int(w * 0.30))
        header_h = 3
        input_h = 3
        footer_h = 2
        main_h = h - header_h - input_h - footer_h

        stdscr.attron(curses.color_pair(1))
        for x in range(w):
            stdscr.addch(header_h - 1, x, " ")
            stdscr.addch(h - input_h - footer_h, x, " ")
            stdscr.addch(h - footer_h, x, " ")
        stdscr.attroff(curses.color_pair(1))

        stdscr.attron(curses.color_pair(2))
        title = f" PYCHAT TUI  |  Usuario: {self.nick}  |  Servidor: {self.host}:{self.port} "
        stdscr.addstr(0, 0, title[:w - 1])
        selected = self._selected_conv()
        stdscr.addstr(1, 0, f" Chat actual: {selected.title} [{selected.kind}] ".ljust(w - 1)[:w - 1])
        stdscr.attroff(curses.color_pair(2))

        stdscr.attron(curses.color_pair(3))
        stdscr.addstr(header_h, 0, " Conversaciones ".ljust(sidebar_w - 1))
        stdscr.attroff(curses.color_pair(3))

        with self.state_lock:
            visible_order = list(self.order)

        y = header_h + 1
        for i, key in enumerate(visible_order):
            if y >= header_h + main_h - 1:
                break
            conv = self.conversations[key]
            unread = f" ({conv.unread})" if conv.unread > 0 else ""
            line = f" {conv.title} [{conv.kind}]{unread}"
            if i == self.selected_index:
                stdscr.attron(curses.color_pair(4))
                stdscr.addstr(y, 0, line.ljust(sidebar_w - 1)[: sidebar_w - 1])
                stdscr.attroff(curses.color_pair(4))
            else:
                stdscr.addstr(y, 0, line[: sidebar_w - 1])
            y += 1

        info_y = header_h + main_h - 5
        if info_y > y:
            stdscr.attron(curses.color_pair(3))
            stdscr.addstr(info_y, 0, " Online ".ljust(sidebar_w - 1)[: sidebar_w - 1])
            stdscr.attroff(curses.color_pair(3))
            users_preview = ", ".join(sorted(self.online_users)) if self.online_users else "(nadie)"
            stdscr.addstr(info_y + 1, 0, users_preview[: sidebar_w - 1])

            rooms_preview = ", ".join(sorted(self.public_rooms)) if self.public_rooms else "(ninguna)"
            stdscr.addstr(info_y + 2, 0, f"Public: {rooms_preview}"[: sidebar_w - 1])

            priv_preview = ", ".join(sorted(self.private_rooms)) if self.private_rooms else "(ninguna)"
            stdscr.addstr(info_y + 3, 0, f"Private: {priv_preview}"[: sidebar_w - 1])

        x0 = sidebar_w + 1
        width_main = w - x0 - 1
        conv = self._selected_conv()
        msgs = conv.messages

        offset = self.scroll_offsets.get(conv.key, 0)
        view_h = main_h - 1
        start = max(0, len(msgs) - view_h - offset)
        end = max(0, len(msgs) - offset)
        visible_msgs = msgs[start:end]

        stdscr.attron(curses.color_pair(3))
        stdscr.addstr(header_h, x0, f" {conv.title} ".ljust(width_main)[:width_main])
        stdscr.attroff(curses.color_pair(3))

        yy = header_h + 1
        for m in visible_msgs:
            wrapped = self.wrap_text(f"[{m.ts}] {m.text}", width_main)
            for piece in wrapped:
                if yy >= header_h + main_h:
                    break
                stdscr.addstr(yy, x0, piece[:width_main])
                yy += 1
            if yy >= header_h + main_h:
                break

        stdscr.attron(curses.color_pair(3))
        stdscr.addstr(h - input_h - footer_h + 1, 0, " Escribe mensaje (soporta /upload /download /viewfiles) ".ljust(w - 1)[: w - 1])
        stdscr.attroff(curses.color_pair(3))

        prompt = "> "
        typed = self.input_buffer
        visible_input = (prompt + typed)[- (w - 2):]
        stdscr.addstr(h - footer_h - 1, 0, visible_input.ljust(w - 1)[: w - 1])

        help_line = "TAB/Shift+TAB o ←/→ cambiar chat | ↑/↓ scroll | Enter enviar | F5 /who | ESC limpiar | /quit salir"
        stdscr.attron(curses.color_pair(2))
        stdscr.addstr(h - footer_h, 0, help_line.ljust(w - 1)[: w - 1])
        transfer_status = self._build_transfer_status()
        footer_status = transfer_status if transfer_status else self.status_line
        stdscr.addstr(h - footer_h + 1, 0, footer_status.ljust(w - 1)[: w - 1])
        stdscr.attroff(curses.color_pair(2))

        self._clear_toast_if_expired()
        with self.state_lock:
            toast = self.toast_text

        if toast:
            toast_w = min(len(toast) + 4, max(20, w // 2))
            toast_x = max(0, w - toast_w - 2)
            toast_y = 2
            stdscr.attron(curses.color_pair(5))
            stdscr.addstr(toast_y, toast_x, (" " + toast + " ")[:toast_w].ljust(toast_w))
            stdscr.attroff(curses.color_pair(5))

        stdscr.move(h - footer_h - 1, min(len(visible_input), w - 2))
        stdscr.refresh()

    def ui_loop(self, stdscr):
        curses.curs_set(1)
        curses.use_default_colors()
        curses.start_color()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_MAGENTA)

        stdscr.nodelay(True)
        stdscr.keypad(True)

        while not self.stop_event.is_set():
            self.draw(stdscr)

            ch = stdscr.getch()
            if ch == -1:
                time.sleep(0.03)
                continue

            if ch in (curses.KEY_RIGHT, 9):
                self._select_next()
                continue
            if ch == curses.KEY_BTAB:
                self._select_prev()
                continue
            if ch == curses.KEY_LEFT:
                self._select_prev()
                continue

            current_key = self._selected_key()
            if ch == curses.KEY_UP:
                self.scroll_offsets[current_key] = min(
                    self.scroll_offsets.get(current_key, 0) + 1,
                    max(0, len(self.conversations[current_key].messages) - 1)
                )
                continue
            if ch == curses.KEY_DOWN:
                self.scroll_offsets[current_key] = max(0, self.scroll_offsets.get(current_key, 0) - 1)
                continue

            if ch == curses.KEY_F5:
                self.send_raw("/who")
                self.status_line = "Refrescando /who..."
                continue

            if ch == 27:  # ESC
                self.input_buffer = ""
                continue

            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.input_buffer = self.input_buffer[:-1]
                continue

            if ch in (10, 13):
                text = self.input_buffer.strip()
                self.input_buffer = ""
                if text:
                    self.send_from_current_conversation(text)
                continue

            if 32 <= ch <= 126 or ch >= 160:
                self.input_buffer += chr(ch)
                continue

        self.close()

    # -------------------------
    # Run
    # -------------------------
    def run(self):
        self.connect()
        self.send_raw("/who")
        self._append_message("SYSTEM", f"Conectado como {self.nick}")
        self._append_message("SYSTEM", "Tips: TAB cambia de chat. /upload /download /viewfiles funcionan en DM/Room.")

        rx = threading.Thread(target=self.receiver_loop, daemon=True)
        rx.start()

        wh = None
        if AUTO_WHO_INTERVAL > 0:
            wh = threading.Thread(target=self.auto_who_loop, daemon=True)
            wh.start()

        try:
            curses.wrapper(self.ui_loop)
        finally:
            self.close()
            rx.join(timeout=1.0)
            if wh:
                wh.join(timeout=1.0)


def main():
    ap = argparse.ArgumentParser(description="Cliente TUI bonito para chat TCP + transferencia de archivos")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--nick", required=True)
    args = ap.parse_args()

    ChatClientTUI(args.host, args.port, args.nick).run()


if __name__ == "__main__":
    main()
