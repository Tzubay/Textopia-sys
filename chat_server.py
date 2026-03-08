#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple, Optional, Set, List, Any

USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,19}$")  # 1..20 chars
ROOMNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,19}$")  # 1..20 chars
MAX_LINE = 8192

# --- File transfer safeguards ---
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 MB (ajusta si quieres)
CHUNK_SIZE = 64 * 1024
WIRE_CHUNK_SIZE = 3 * 1024


@dataclass
class Client:
    username: str
    sock: socket.socket
    addr: Tuple[str, int]
    out_q: "queue.Queue[bytes]"
    alive: threading.Event
    send_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Room:
    name: str
    status: str  # "PUBLIC" | "PRIVATE"
    owner: str
    members: Set[str] = field(default_factory=set)


@dataclass
class UploadSession:
    transfer_id: str
    sender: str
    targets: List[str]
    safe_name: str
    size: int
    remaining: int
    tmp_path: Path
    sha: "hashlib._Hash"


class ChatServer:
    def __init__(self, host: str, port: int, files_root: str = "server_files"):
        self.host = host
        self.port = port

        self.clients: Dict[str, Client] = {}
        self.clients_lock = threading.RLock()

        self.rooms: Dict[str, Room] = {}
        self.rooms_lock = threading.RLock()

        self.user_active_room: Dict[str, str] = {}
        self.user_ctx_lock = threading.RLock()

        self.stop_event = threading.Event()

        self.files_root = Path(files_root)
        (self.files_root / "_tmp").mkdir(parents=True, exist_ok=True)
        (self.files_root / "dms").mkdir(parents=True, exist_ok=True)
        (self.files_root / "rooms").mkdir(parents=True, exist_ok=True)

    # ---------- Utilidades de envío ----------
    @staticmethod
    def _encode_line(text: str) -> bytes:
        return (text.rstrip("\r\n") + "\n").encode("utf-8", errors="replace")

    def _client_send_direct(self, client: Client, payload: bytes) -> None:
        """Envío directo (respeta send_lock para no mezclar con writer)."""
        with client.send_lock:
            client.sock.sendall(payload)

    def send_to(self, username: str, text: str) -> bool:
        with self.clients_lock:
            c = self.clients.get(username)
            if not c:
                return False
            c.out_q.put(self._encode_line(text))
            return True

    def broadcast(self, text: str, exclude: Optional[str] = None) -> None:
        payload = self._encode_line(text)
        with self.clients_lock:
            for u, c in self.clients.items():
                if exclude is not None and u == exclude:
                    continue
                c.out_q.put(payload)

    def room_broadcast(self, room_name: str, text: str, exclude: Optional[str] = None) -> None:
        payload = self._encode_line(text)
        with self.rooms_lock:
            room = self.rooms.get(room_name)
            if not room:
                return
            members = list(room.members)

        with self.clients_lock:
            for u in members:
                if exclude is not None and u == exclude:
                    continue
                c = self.clients.get(u)
                if c:
                    c.out_q.put(payload)

    def list_users(self) -> str:
        with self.clients_lock:
            users = sorted(self.clients.keys())
        return ", ".join(users) if users else "(nadie)"

    # ---------- Lectura por líneas (buffered) ----------
    @staticmethod
    def recv_line(sock: socket.socket, buffer: bytearray) -> Optional[str]:
        while True:
            nl = buffer.find(b"\n")
            if nl != -1:
                line = buffer[:nl]
                del buffer[: nl + 1]
                if len(line) > MAX_LINE:
                    raise ValueError("Línea demasiado larga")
                return line.decode("utf-8", errors="replace").rstrip("\r")

            chunk = sock.recv(4096)
            if not chunk:
                return None
            buffer.extend(chunk)
            if len(buffer) > MAX_LINE * 2:
                raise ValueError("Buffer demasiado grande")

    @staticmethod
    def read_exact(sock: socket.socket, buffer: bytearray, n: int) -> bytes:
        """Lee exactamente n bytes, consumiendo primero de buffer."""
        out = bytearray()
        if n <= 0:
            return b""

        # Consumir del buffer residual primero
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

    # ---------- Rooms: helpers ----------
    def _set_active_room(self, username: str, room: str) -> None:
        with self.user_ctx_lock:
            self.user_active_room[username] = room

    def _get_active_room(self, username: str) -> Optional[str]:
        with self.user_ctx_lock:
            return self.user_active_room.get(username)

    @staticmethod
    def _parse_bracket_list(s: str) -> List[str]:
        s = s.strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1].strip()
        if not s:
            return []
        parts = [p.strip() for p in s.split(",")]
        return [p for p in parts if p]

    def _create_room(self, owner: str, name: str, status: str, invitees: List[str]) -> str:
        status = status.upper()
        if status not in ("PUBLIC", "PRIVATE"):
            return "* status inválido. Usa PUBLIC o PRIVATE."

        if not ROOMNAME_RE.match(name):
            return "* Nombre de sala inválido (1-20 chars: letras/números/_/-)."

        with self.clients_lock:
            if name in self.clients:
                return "* Ese nombre coincide con un usuario conectado. Elige otro."

        with self.rooms_lock:
            if name in self.rooms:
                return "* Ya existe una sala con ese nombre."

            room = Room(name=name, status=status, owner=owner)
            room.members.add(owner)

            added = []
            skipped_self = False
            invalids = []
            for u in invitees:
                if u == owner:
                    skipped_self = True
                    continue
                if not USERNAME_RE.match(u):
                    invalids.append(u)
                    continue
                room.members.add(u)
                added.append(u)

            self.rooms[name] = room

        self._set_active_room(owner, name)
        self.send_to(owner, f"* Sala creada: {name} ({status}). Owner: {owner}. Miembros: {len(room.members)}")
        if skipped_self:
            self.send_to(owner, "* Nota: te omití de la lista de invitación porque ya eres el creador.")
        if invalids:
            self.send_to(owner, f"* Invitados ignorados por nick inválido: {', '.join(invalids)}")

        for u in added:
            self.send_to(u, f"* Te agregaron a la sala '{name}' ({status}) por {owner}. Envia: @{name} tu mensaje")

        return "* OK"

    def _join_public_room(self, username: str, room_name: str) -> str:
        with self.rooms_lock:
            room = self.rooms.get(room_name)
            if not room:
                return "* Esa sala no existe."
            if room.status != "PUBLIC":
                return "* Esa sala es PRIVATE. No puedes entrar con /intro."
            already = username in room.members
            room.members.add(username)

        self._set_active_room(username, room_name)
        if not already:
            self.room_broadcast(room_name, f"* {username} se unió a la sala {room_name}.", exclude=None)
        return f"* Ahora estás en '{room_name}'. Envia mensajes con @{room_name} ..."

    def _quit_room(self, username: str, room_name: str) -> str:
        with self.rooms_lock:
            room = self.rooms.get(room_name)
            if not room:
                return "* Esa sala no existe."
            if username not in room.members:
                return "* No estás en esa sala."

            room.members.remove(username)
            remaining = len(room.members)

            if remaining == 0:
                del self.rooms[room_name]
                return f"* Saliste de '{room_name}' y la sala se eliminó (quedó vacía)."

        self.room_broadcast(room_name, f"* {username} salió permanentemente de la sala {room_name}.", exclude=None)
        return f"* Saliste permanentemente de '{room_name}'."

    def _invite_to_room(self, inviter: str, room_name: str, invitees: List[str]) -> str:
        with self.rooms_lock:
            room = self.rooms.get(room_name)
            if not room:
                return "* Esa sala no existe."
            if inviter not in room.members:
                return "* No puedes invitar: no estás en esa sala."

            added = []
            invalids = []
            skipped_self = False
            for u in invitees:
                if u == inviter:
                    skipped_self = True
                    continue
                if not USERNAME_RE.match(u):
                    invalids.append(u)
                    continue
                if u not in room.members:
                    room.members.add(u)
                    added.append(u)

        if skipped_self:
            self.send_to(inviter, "* Nota: te omití porque ya estás en la sala.")
        if invalids:
            self.send_to(inviter, f"* Invitados ignorados por nick inválido: {', '.join(invalids)}")

        if not added:
            return "* Nadie nuevo fue agregado (ya estaban o eran inválidos)."

        for u in added:
            self.send_to(u, f"* Te agregaron a la sala '{room_name}' ({room.status}) por {inviter}. Envia: @{room_name} ...")

        self.room_broadcast(room_name, f"* {inviter} agregó a: {', '.join(added)}", exclude=None)
        return "* OK"

    def _format_rooms_for_who(self, username: str) -> str:
        with self.rooms_lock:
            publics = []
            privs = []
            for r in self.rooms.values():
                if r.status == "PUBLIC":
                    publics.append(r.name)
                else:
                    if username in r.members:
                        privs.append(r.name)

        publics.sort()
        privs.sort()

        parts = []
        parts.append(f"* Conectados: {self.list_users()}")
        parts.append(f"* Salas PUBLIC: {', '.join(publics) if publics else '(ninguna)'}")
        parts.append(f"* Tus salas PRIVATE (solo si eres miembro): {', '.join(privs) if privs else '(ninguna)'}")
        ar = self._get_active_room(username)
        if ar:
            parts.append(f"* Sala activa: {ar}")
        return "\n".join(parts)

    # ---------- File transfer helpers ----------
    @staticmethod
    def _sanitize_filename(name: str) -> str:
        # quedarnos con basename y reemplazar caracteres raros
        name = os.path.basename(name)
        if not name:
            return "file"
        # permitir letras, números, espacio, _, -, ., y paréntesis
        safe = []
        for ch in name:
            if ch.isalnum() or ch in " _-().,[]":
                safe.append(ch)
            elif ch in "/\\":
                continue
            else:
                safe.append("_")
        out = "".join(safe).strip()
        return out[:120] if out else "file"

    @staticmethod
    def _format_size(n: int) -> str:
        # formato humano simple
        units = ["B", "KB", "MB", "GB"]
        v = float(n)
        for u in units:
            if v < 1024.0 or u == units[-1]:
                if u == "B":
                    return f"{int(v)} {u}"
                return f"{v:.1f} {u}"
            v /= 1024.0
        return f"{n} B"

    def _dm_scope_dir(self, a: str, b: str) -> Path:
        x, y = sorted([a, b])
        return self.files_root / "dms" / f"{x}__{y}"

    def _room_scope_dir(self, room: str) -> Path:
        return self.files_root / "rooms" / room

    def _unique_name(self, folder: Path, filename: str) -> str:
        folder.mkdir(parents=True, exist_ok=True)
        base = filename
        stem, ext = os.path.splitext(base)
        candidate = base
        k = 1
        while (folder / candidate).exists():
            k += 1
            candidate = f"{stem}({k}){ext}"
        return candidate

    def _append_manifest(self, folder: Path, entry: Dict[str, Any]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        mf = folder / "manifest.jsonl"
        with mf.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _read_manifest(self, folder: Path) -> List[Dict[str, Any]]:
        mf = folder / "manifest.jsonl"
        if not mf.exists():
            return []
        out: List[Dict[str, Any]] = []
        with mf.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def _is_room_member(self, username: str, room_name: str) -> bool:
        with self.rooms_lock:
            room = self.rooms.get(room_name)
            if not room:
                return False
            return username in room.members

    def _finalize_uploaded_file(self, sender: str, targets: List[str], safe_name: str, size: int, tmp_path: Path, digest: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()

        dm_peers: List[str] = []
        rooms: List[str] = []
        unknown: List[str] = []

        with self.rooms_lock:
            room_names = set(self.rooms.keys())

        for t in targets:
            if not isinstance(t, str):
                continue
            t = t.strip()
            if not t:
                continue
            if t.startswith("@"):
                u = t[1:]
                if USERNAME_RE.match(u):
                    if u != sender:
                        dm_peers.append(u)
                else:
                    unknown.append(t)
                continue
            if t in room_names:
                rooms.append(t)
                continue
            if USERNAME_RE.match(t):
                if t != sender:
                    dm_peers.append(t)
            else:
                unknown.append(t)

        dm_peers = sorted(set(dm_peers))
        rooms = sorted(set(rooms))

        if unknown:
            self.send_to(sender, f"* (upload) Destinos ignorados (inválidos/no existen): {', '.join(unknown)}")

        if not dm_peers and not rooms:
            self.send_to(sender, "* (upload) No hay destinos válidos.")
            return

        ok_rooms: List[str] = []
        for r in rooms:
            if self._is_room_member(sender, r):
                ok_rooms.append(r)
            else:
                self.send_to(sender, f"* (upload) No puedes subir a '{r}': no eres miembro.")
        rooms = ok_rooms

        if not dm_peers and not rooms:
            self.send_to(sender, "* (upload) No quedó ningún destino permitido.")
            return

        human_size = self._format_size(size)

        for peer in dm_peers:
            scope_dir = self._dm_scope_dir(sender, peer)
            stored = self._unique_name(scope_dir, safe_name)
            shutil.copy2(tmp_path, scope_dir / stored)
            self._append_manifest(scope_dir, {
                "stored": stored,
                "original": safe_name,
                "from": sender,
                "to": peer,
                "kind": "DM",
                "size": size,
                "sha256": digest,
                "ts": ts,
            })
            self.send_to(peer, f"[DM de {sender}] (archivo) {sender} mandó '{stored}' ({human_size}). Usa /download {stored} <ruta>")
            self.send_to(sender, f"[DM a {peer}] (archivo) enviado '{stored}' ({human_size}).")

        for room in rooms:
            scope_dir = self._room_scope_dir(room)
            stored = self._unique_name(scope_dir, safe_name)
            shutil.copy2(tmp_path, scope_dir / stored)
            self._append_manifest(scope_dir, {
                "stored": stored,
                "original": safe_name,
                "from": sender,
                "room": room,
                "kind": "ROOM",
                "size": size,
                "sha256": digest,
                "ts": ts,
            })
            self.room_broadcast(room, f"[Room:{room}] {sender}: (archivo) mandó '{stored}' ({human_size}). Usa /download {stored} <ruta>", exclude=None)

    def _handle_fileupload_begin(self, sender: str, upload_sessions: Dict[str, UploadSession], payload_json: str) -> None:
        try:
            meta = json.loads(payload_json)
        except json.JSONDecodeError:
            self.send_to(sender, "* (upload) Formato inválido.")
            return

        transfer_id = str(meta.get("id", "")).strip()
        targets = meta.get("targets")
        filename = meta.get("filename")
        size = meta.get("size")
        if not transfer_id or not isinstance(targets, list) or not isinstance(filename, str) or not isinstance(size, int):
            self.send_to(sender, "* (upload) Datos incompletos.")
            return
        if transfer_id in upload_sessions:
            self.send_to(sender, "* (upload) id de transferencia duplicado.")
            return
        if size < 0 or size > MAX_FILE_SIZE:
            self.send_to(sender, f"* (upload) Tamaño inválido o excede el límite ({self._format_size(MAX_FILE_SIZE)}).")
            return

        safe_name = self._sanitize_filename(filename)
        tmp_name = f"{int(time.time()*1000)}_{sender}_{safe_name}"
        tmp_path = self.files_root / "_tmp" / tmp_name
        try:
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.touch()
        except Exception:
            self.send_to(sender, "* (upload) No se pudo preparar el archivo temporal.")
            return

        upload_sessions[transfer_id] = UploadSession(
            transfer_id=transfer_id,
            sender=sender,
            targets=targets,
            safe_name=safe_name,
            size=size,
            remaining=size,
            tmp_path=tmp_path,
            sha=hashlib.sha256(),
        )

    def _handle_fileupload_chunk(self, sender: str, upload_sessions: Dict[str, UploadSession], payload_json: str) -> None:
        try:
            meta = json.loads(payload_json)
            transfer_id = str(meta.get("id", "")).strip()
            data_b64 = str(meta.get("data", ""))
        except Exception:
            self.send_to(sender, "* (upload) Chunk inválido.")
            return
        s = upload_sessions.get(transfer_id)
        if not s or s.sender != sender:
            self.send_to(sender, "* (upload) Sesión no encontrada.")
            return
        try:
            data = base64.b64decode(data_b64.encode("ascii"), validate=True)
        except Exception:
            self.send_to(sender, "* (upload) Chunk corrupto.")
            return

        if len(data) > s.remaining:
            self.send_to(sender, "* (upload) Tamaño excedido.")
            try:
                s.tmp_path.unlink(missing_ok=True)  # type: ignore
            except Exception:
                pass
            upload_sessions.pop(transfer_id, None)
            return

        try:
            with s.tmp_path.open("ab") as f:
                f.write(data)
            s.sha.update(data)
            s.remaining -= len(data)
        except Exception:
            self.send_to(sender, "* (upload) Error escribiendo chunk.")
            try:
                s.tmp_path.unlink(missing_ok=True)  # type: ignore
            except Exception:
                pass
            upload_sessions.pop(transfer_id, None)

    def _handle_fileupload_end(self, sender: str, upload_sessions: Dict[str, UploadSession], payload_json: str) -> None:
        try:
            meta = json.loads(payload_json)
            transfer_id = str(meta.get("id", "")).strip()
        except Exception:
            self.send_to(sender, "* (upload) END inválido.")
            return

        s = upload_sessions.pop(transfer_id, None)
        if not s or s.sender != sender:
            self.send_to(sender, "* (upload) Sesión no encontrada.")
            return

        if s.remaining != 0:
            self.send_to(sender, "* (upload) Transferencia incompleta.")
            try:
                s.tmp_path.unlink(missing_ok=True)  # type: ignore
            except Exception:
                pass
            return

        try:
            self._finalize_uploaded_file(sender, s.targets, s.safe_name, s.size, s.tmp_path, s.sha.hexdigest())
        finally:
            try:
                s.tmp_path.unlink(missing_ok=True)  # type: ignore
            except Exception:
                pass

    def _handle_fileview(self, requester: str, client: Client, payload_json: str) -> None:
        try:
            meta = json.loads(payload_json)
        except json.JSONDecodeError:
            self.send_to(requester, "* (viewfiles) Formato inválido.")
            return

        kind = str(meta.get("kind", "")).upper()
        limit = meta.get("limit", 0)
        if not isinstance(limit, int) or limit < 0:
            limit = 0

        scope_key = ""
        folder: Optional[Path] = None

        if kind == "ROOM":
            room = meta.get("room")
            if not isinstance(room, str) or not room:
                self.send_to(requester, "* (viewfiles) Falta room.")
                return
            if not self._is_room_member(requester, room):
                self.send_to(requester, "* (viewfiles) No eres miembro de esa sala.")
                return
            scope_key = f"ROOM:{room}"
            folder = self._room_scope_dir(room)

        elif kind == "DM":
            peer = meta.get("peer")
            if not isinstance(peer, str) or not peer:
                self.send_to(requester, "* (viewfiles) Falta peer.")
                return
            if not USERNAME_RE.match(peer) or peer == requester:
                self.send_to(requester, "* (viewfiles) peer inválido.")
                return
            scope_key = f"DM:{peer}"
            folder = self._dm_scope_dir(requester, peer)

        else:
            self.send_to(requester, "* (viewfiles) kind inválido.")
            return

        items = self._read_manifest(folder)
        if limit > 0:
            items = items[-limit:]

        # Responder como protocolo para que el cliente lo ponga en la conversación correcta
        begin = {"scope": scope_key, "count": len(items)}
        self._client_send_direct(client, self._encode_line("FILELISTBEGIN " + json.dumps(begin, ensure_ascii=False)))

        for it in items:
            row = {
                "scope": scope_key,
                "filename": it.get("stored"),
                "size": it.get("size"),
                "from": it.get("from"),
                "ts": it.get("ts"),
            }
            self._client_send_direct(client, self._encode_line("FILEITEM " + json.dumps(row, ensure_ascii=False)))

        self._client_send_direct(client, self._encode_line("FILELISTEND " + json.dumps({"scope": scope_key}, ensure_ascii=False)))

    def _handle_filedownload(self, requester: str, client: Client, payload_json: str) -> None:
        try:
            meta = json.loads(payload_json)
        except json.JSONDecodeError:
            self.send_to(requester, "* (download) Formato inválido.")
            return

        kind = str(meta.get("kind", "")).upper()
        filename = meta.get("filename")
        if not isinstance(filename, str) or not filename:
            self.send_to(requester, "* (download) Falta filename.")
            return

        folder: Optional[Path] = None
        scope_key = ""

        if kind == "ROOM":
            room = meta.get("room")
            if not isinstance(room, str) or not room:
                self.send_to(requester, "* (download) Falta room.")
                return
            if not self._is_room_member(requester, room):
                self.send_to(requester, "* (download) No eres miembro de esa sala.")
                return
            folder = self._room_scope_dir(room)
            scope_key = f"ROOM:{room}"

        elif kind == "DM":
            peer = meta.get("peer")
            if not isinstance(peer, str) or not peer:
                self.send_to(requester, "* (download) Falta peer.")
                return
            if not USERNAME_RE.match(peer) or peer == requester:
                self.send_to(requester, "* (download) peer inválido.")
                return
            folder = self._dm_scope_dir(requester, peer)
            scope_key = f"DM:{peer}"

        else:
            self.send_to(requester, "* (download) kind inválido.")
            return

        safe = self._sanitize_filename(filename)
        path = folder / safe
        if not path.exists() or not path.is_file():
            err = {"scope": scope_key, "error": "Archivo no existe"}
            self._client_send_direct(client, self._encode_line("FILEERR " + json.dumps(err, ensure_ascii=False)))
            return

        size = path.stat().st_size
        if size > MAX_FILE_SIZE:
            err = {"scope": scope_key, "error": "Archivo excede límite del servidor"}
            self._client_send_direct(client, self._encode_line("FILEERR " + json.dumps(err, ensure_ascii=False)))
            return

        transfer_id = f"down-{int(time.time() * 1000)}-{requester}"

        def worker():
            begin = {"id": transfer_id, "scope": scope_key, "filename": safe, "size": size}
            client.out_q.put(self._encode_line("FILEDOWNLOADBEGIN " + json.dumps(begin, ensure_ascii=False)))
            try:
                with path.open("rb") as f:
                    while True:
                        raw = f.read(WIRE_CHUNK_SIZE)
                        if not raw:
                            break
                        row = {
                            "id": transfer_id,
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                        client.out_q.put(self._encode_line("FILEDOWNLOADCHUNK " + json.dumps(row, ensure_ascii=False)))
                client.out_q.put(self._encode_line("FILEDOWNLOADEND " + json.dumps({"id": transfer_id}, ensure_ascii=False)))
            except Exception:
                err = {"id": transfer_id, "scope": scope_key, "error": "Descarga interrumpida"}
                client.out_q.put(self._encode_line("FILEERR " + json.dumps(err, ensure_ascii=False)))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Writer thread ----------
    def client_writer(self, client: Client) -> None:
        try:
            while client.alive.is_set() and not self.stop_event.is_set():
                try:
                    data = client.out_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    with client.send_lock:
                        client.sock.sendall(data)
                except OSError:
                    break
        finally:
            pass

    # ---------- Disconnect ----------
    def disconnect(self, username: str, reason: str = "desconectado") -> None:
        with self.clients_lock:
            client = self.clients.pop(username, None)

        if client:
            client.alive.clear()
            try:
                client.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.sock.close()
            except OSError:
                pass

        with self.user_ctx_lock:
            self.user_active_room.pop(username, None)

        self.broadcast(f"* {username} {reason}.", exclude=None)

        with self.rooms_lock:
            room_names = [r.name for r in self.rooms.values() if username in r.members]
        for rn in room_names:
            self.room_broadcast(rn, f"* {username} se desconectó (sigue siendo miembro).", exclude=None)

    # ---------- Comandos rooms ----------
    def _handle_room_command(self, sender: str, line: str) -> None:
        rest = line[len("/room"):].strip()
        if not rest:
            self.send_to(sender, "* Uso: /room [nick2,nick3] name <Sala> status <PUBLIC|PRIVATE>")
            return

        invitees: List[str] = []
        if "[" in rest and "]" in rest:
            lb = rest.find("[")
            rb = rest.find("]", lb + 1)
            bracket = rest[lb: rb + 1]
            invitees = self._parse_bracket_list(bracket)
            rest2 = (rest[:lb] + rest[rb + 1:]).strip()
        else:
            rest2 = rest

        m = re.search(r"\bname\s+(\S+)\s+status\s+(PUBLIC|PRIVATE)\b", rest2, re.IGNORECASE)
        if not m:
            self.send_to(sender, "* Uso: /room [nick2,nick3] name <Sala> status <PUBLIC|PRIVATE>")
            return

        room_name = m.group(1).strip()
        status = m.group(2).upper()

        msg = self._create_room(sender, room_name, status, invitees)
        self.send_to(sender, msg)

    def _handle_invite_command(self, sender: str, line: str) -> None:
        rest = line[len("/invite"):].strip()
        if not rest:
            self.send_to(sender, "* Uso: /invite [nick] room <Sala>  (o sin room usa tu sala activa)")
            return

        invitees: List[str] = []
        room_name: Optional[str] = None

        if rest.startswith("["):
            rb = rest.find("]")
            if rb == -1:
                self.send_to(sender, "* Lista inválida. Ej: /invite [nick7,nick8] room Amigos")
                return
            bracket = rest[: rb + 1]
            invitees = self._parse_bracket_list(bracket)
            tail = rest[rb + 1:].strip()
        else:
            parts = rest.split()
            invitees = [parts[0].strip("[]")]
            tail = " ".join(parts[1:]).strip()

        m = re.search(r"\broom\s+(\S+)\b", tail, re.IGNORECASE)
        if m:
            room_name = m.group(1).strip()

        if not room_name:
            room_name = self._get_active_room(sender)

        if not room_name:
            self.send_to(sender, "* No indicaste sala y no tienes sala activa. Usa: /invite [nick] room <Sala>")
            return

        if not invitees:
            self.send_to(sender, "* No indicaste a quién invitar.")
            return

        msg = self._invite_to_room(sender, room_name, invitees)
        self.send_to(sender, msg)

    def _handle_intro_command(self, sender: str, line: str) -> None:
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            self.send_to(sender, "* Uso: /intro <SalaPublica>")
            return
        room_name = parts[1].strip()
        msg = self._join_public_room(sender, room_name)
        self.send_to(sender, msg)

    def _handle_quitroom_command(self, sender: str, line: str) -> None:
        parts = line.split(maxsplit=1)
        if len(parts) >= 2:
            room_name = parts[1].strip()
        else:
            room_name = self._get_active_room(sender) or ""

        if not room_name:
            self.send_to(sender, "* Uso: /quitroom <Sala>  (o sin args usa tu sala activa)")
            return

        msg = self._quit_room(sender, room_name)
        self.send_to(sender, msg)

        if self._get_active_room(sender) == room_name:
            with self.user_ctx_lock:
                self.user_active_room.pop(sender, None)

    # ---------- Mensajes ----------
    def handle_message(self, sender: str, line: str) -> None:
        line = line.strip()
        if not line:
            return

        if line.startswith("/"):
            cmd = line.split(maxsplit=1)[0].lower()

            if cmd == "/who":
                self.send_to(sender, self._format_rooms_for_who(sender))
                return

            if cmd == "/help":
                self.send_to(sender, "* Comandos: /who, /msg <user> <msg>, /all <msg>, /room [...], /invite [...], /intro <Sala>, /quitroom <Sala>, /help")
                self.send_to(sender, "* DM: @usuario mensaje   | Sala: @Sala mensaje")
                self.send_to(sender, "* Archivos (cliente TUI): /upload [...], /download <archivo> <ruta>, /viewfiles [N]")
                return

            if cmd == "/all":
                msg = line[len("/all"):].strip()
                if msg:
                    self.broadcast(f"[{sender}] {msg}", exclude=None)
                return

            if cmd == "/msg":
                parts = line.split(maxsplit=2)
                if len(parts) < 3:
                    self.send_to(sender, "* Uso: /msg usuario mensaje")
                    return
                target, msg = parts[1], parts[2]
                if target == sender:
                    self.send_to(sender, "* No puedes mandarte DM a ti mismo.")
                    return
                if not self.send_to(target, f"[DM de {sender}] {msg}"):
                    self.send_to(sender, f"* Usuario '{target}' no existe o no está conectado.")
                else:
                    self.send_to(sender, f"[DM a {target}] {msg}")
                return

            if cmd == "/room":
                self._handle_room_command(sender, line)
                return

            if cmd == "/invite":
                self._handle_invite_command(sender, line)
                return

            if cmd == "/intro":
                self._handle_intro_command(sender, line)
                return

            if cmd == "/quitroom":
                self._handle_quitroom_command(sender, line)
                return

            self.send_to(sender, "* Comando no reconocido. Usa /help")
            return

        # Mensaje con @destino ...
        if line.startswith("@"):
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                self.send_to(sender, "* Uso: @usuario mensaje  o  @Sala mensaje")
                return
            target = parts[0][1:]
            msg = parts[1]

            with self.clients_lock:
                is_user = target in self.clients

            if is_user:
                if target == sender:
                    self.send_to(sender, "* No puedes mandarte DM a ti mismo.")
                    return
                if not self.send_to(target, f"[DM de {sender}] {msg}"):
                    self.send_to(sender, f"* Usuario '{target}' no existe o no está conectado.")
                else:
                    self.send_to(sender, f"[DM a {target}] {msg}")
                return

            with self.rooms_lock:
                room = self.rooms.get(target)

            if not room:
                self.send_to(sender, f"* No existe usuario conectado ni sala llamada '{target}'.")
                return

            if room.status == "PRIVATE" and sender not in room.members:
                self.send_to(sender, f"* La sala '{target}' es PRIVATE y no eres miembro.")
                return

            if room.status == "PUBLIC" and sender not in room.members:
                self.send_to(sender, f"* No estás en '{target}'. Entra con: /intro {target}")
                return

            self._set_active_room(sender, target)
            formatted = f"[Room:{target}] {sender}: {msg}"
            self.send_to(sender, formatted)
            self.room_broadcast(target, formatted, exclude=sender)
            return

        self.broadcast(f"[{sender}] {line}", exclude=None)

    # ---------- Manejo de cliente ----------
    def client_reader(self, sock: socket.socket, addr: Tuple[str, int]) -> None:
        buffer = bytearray()
        upload_sessions: Dict[str, UploadSession] = {}
        username: Optional[str] = None
        alive = threading.Event()
        alive.set()
        out_q: "queue.Queue[bytes]" = queue.Queue()

        try:
            sock.settimeout(30.0)
            sock.sendall(self._encode_line("* Bienvenido. Escribe: NICK tu_nombre"))

            while not self.stop_event.is_set():
                line = self.recv_line(sock, buffer)
                if line is None:
                    return
                line = line.strip()
                if not line:
                    continue

                if line.upper().startswith("NICK "):
                    proposed = line.split(maxsplit=1)[1].strip()
                    if not USERNAME_RE.match(proposed):
                        sock.sendall(self._encode_line("* Username inválido. Usa 1-20 chars: letras/números/_/-"))
                        continue

                    with self.clients_lock:
                        if proposed in self.clients:
                            sock.sendall(self._encode_line("* Ese username ya está en uso. Prueba otro."))
                            continue
                        username = proposed
                        client = Client(username=username, sock=sock, addr=addr, out_q=out_q, alive=alive)
                        self.clients[username] = client

                    writer_t = threading.Thread(target=self.client_writer, args=(client,), daemon=True)
                    writer_t.start()

                    self.send_to(username, f"* Conectado como '{username}'. Escribe /help para comandos.")
                    self.broadcast(f"* {username} se unió al chat.", exclude=username)

                    with self.rooms_lock:
                        my_rooms = [r.name for r in self.rooms.values() if username in r.members]
                    if my_rooms:
                        self.send_to(username, f"* Ya eres miembro de: {', '.join(sorted(my_rooms))}")
                        self._set_active_room(username, sorted(my_rooms)[0])

                    break
                else:
                    sock.sendall(self._encode_line("* Primero define tu nombre: NICK tu_nombre"))

            if username is None:
                return

            sock.settimeout(1.0)

            while alive.is_set() and not self.stop_event.is_set():
                try:
                    line = self.recv_line(sock, buffer)
                except socket.timeout:
                    continue

                if line is None:
                    break

                # --- Protocolos de archivos (en línea) ---
                if line.startswith("FILEUPLOADBEGIN "):
                    payload = line[len("FILEUPLOADBEGIN "):].strip()
                    self._handle_fileupload_begin(username, upload_sessions, payload)
                    continue

                if line.startswith("FILEUPLOADCHUNK "):
                    payload = line[len("FILEUPLOADCHUNK "):].strip()
                    self._handle_fileupload_chunk(username, upload_sessions, payload)
                    continue

                if line.startswith("FILEUPLOADEND "):
                    payload = line[len("FILEUPLOADEND "):].strip()
                    self._handle_fileupload_end(username, upload_sessions, payload)
                    continue

                if line.startswith("FILEVIEW "):
                    payload = line[len("FILEVIEW "):].strip()
                    with self.clients_lock:
                        c = self.clients.get(username)
                    if c:
                        self._handle_fileview(username, c, payload)
                    continue

                if line.startswith("FILEDOWNLOAD "):
                    payload = line[len("FILEDOWNLOAD "):].strip()
                    with self.clients_lock:
                        c = self.clients.get(username)
                    if c:
                        self._handle_filedownload(username, c, payload)
                    continue

                # Mensaje normal
                try:
                    self.handle_message(username, line)
                except Exception:
                    self.send_to(username, "* Error procesando tu mensaje.")

        except (OSError, ValueError):
            pass
        finally:
            for s in upload_sessions.values():
                try:
                    s.tmp_path.unlink(missing_ok=True)  # type: ignore
                except Exception:
                    pass
            if username is not None:
                self.disconnect(username, reason="salió")
            else:
                try:
                    sock.close()
                except OSError:
                    pass

    # ---------- Loop principal ----------
    def serve_forever(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(200)
        srv.settimeout(1.0)

        print(f"Servidor escuchando en {self.host}:{self.port}")
        try:
            while not self.stop_event.is_set():
                try:
                    sock, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                t = threading.Thread(target=self.client_reader, args=(sock, addr), daemon=True)
                t.start()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            try:
                srv.close()
            except OSError:
                pass

            with self.clients_lock:
                users = list(self.clients.keys())
            for u in users:
                self.disconnect(u, reason="fue desconectado (servidor cerrando)")

            print("Servidor cerrado.")


def main():
    ap = argparse.ArgumentParser(description="Servidor de chat (TCP + threads + rooms + file transfer)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--files", default="server_files", help="Carpeta raíz para archivos")
    args = ap.parse_args()

    ChatServer(args.host, args.port, files_root=args.files).serve_forever()


if __name__ == "__main__":
    main()
