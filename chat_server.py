#!/usr/bin/env python3
import argparse
import queue
import re
import socket
import threading
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional, Set, List

USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,19}$")  # 1..20
ROOMNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,19}$")  # 1..20
MAX_LINE = 8192  # bytes por línea (anti-abuso simple)


@dataclass
class Client:
    username: str
    sock: socket.socket
    addr: Tuple[str, int]
    out_q: "queue.Queue[bytes]"
    alive: threading.Event


@dataclass
class Room:
    name: str
    status: str  # "PUBLIC" | "PRIVATE"
    owner: str
    members: Set[str] = field(default_factory=set)  # usernames (pueden estar offline)


class ChatServer:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

        self.clients: Dict[str, Client] = {}
        self.clients_lock = threading.RLock()

        self.rooms: Dict[str, Room] = {}
        self.rooms_lock = threading.RLock()

        # “Sala activa” por usuario (para /invite sin room y /quitroom sin args)
        self.user_active_room: Dict[str, str] = {}
        self.user_ctx_lock = threading.RLock()

        self.stop_event = threading.Event()

    # ---------- Utilidades ----------
    @staticmethod
    def _encode_line(text: str) -> bytes:
        return (text.rstrip("\r\n") + "\n").encode("utf-8", errors="replace")

    @staticmethod
    def _parse_bracket_list(s: str) -> List[str]:
        """
        Acepta: "[a,b,c]" o "a,b,c" o "[a, b, c]"
        Devuelve lista limpia sin vacíos.
        """
        s = s.strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1].strip()
        if not s:
            return []
        parts = [p.strip() for p in s.split(",")]
        return [p for p in parts if p]

    def _set_active_room(self, username: str, room: str) -> None:
        with self.user_ctx_lock:
            self.user_active_room[username] = room

    def _get_active_room(self, username: str) -> Optional[str]:
        with self.user_ctx_lock:
            return self.user_active_room.get(username)

    # ---------- Envío ----------
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
        """
        Envía a todos los miembros *conectados* de la sala.
        La membresía puede incluir offline; se filtra por self.clients.
        """
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

    # ---------- Sockets: lectura por líneas ----------
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

    # ---------- Rooms: helpers ----------
    def _room_visible_to(self, room: Room, username: str) -> bool:
        if room.status == "PUBLIC":
            return True
        return username in room.members

    def _room_exists(self, name: str) -> bool:
        with self.rooms_lock:
            return name in self.rooms

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

            # Invitados (pueden estar offline, pero deben tener nick válido)
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

        # Notificar
        self._set_active_room(owner, name)
        self.send_to(owner, f"* Sala creada: {name} ({status}). Owner: {owner}. Miembros: {len(room.members)}")
        if skipped_self:
            self.send_to(owner, "* Nota: te omití de la lista de invitación porque ya eres el creador.")

        if invalids:
            self.send_to(owner, f"* Invitados ignorados por nick inválido: {', '.join(invalids)}")

        # Avisar a invitados conectados
        for u in added:
            if self.send_to(u, f"* Te agregaron a la sala '{name}' ({status}) por {owner}. Envia: @{name} tu mensaje"):
                pass

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

            # Si queda vacía, se borra
            if remaining == 0:
                del self.rooms[room_name]
                return f"* Saliste de '{room_name}' y la sala se eliminó (quedó vacía)."

        # Notificar a conectados de esa sala
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

        # Notificar a invitados (si están conectados)
        for u in added:
            if self.send_to(u, f"* Te agregaron a la sala '{room_name}' ({room.status}) por {inviter}. Envia: @{room_name} ..."):
                pass
            else:
                # offline: no pasa nada, pero queda como miembro para cuando se conecte con ese nick
                pass

        # Notificar a conectados del room (opcional, solo si quieres)
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

    # ---------- Writer thread ----------
    def client_writer(self, client: Client) -> None:
        try:
            while client.alive.is_set() and not self.stop_event.is_set():
                try:
                    data = client.out_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    client.sock.sendall(data)
                except OSError:
                    break
        finally:
            pass

    # ---------- Disconnect ----------
    def disconnect(self, username: str, reason: str = "desconectado") -> None:
        # Cerrar socket/cliente
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

        # Anunciar en global
        self.broadcast(f"* {username} {reason}.", exclude=None)

        # Anunciar en salas donde sea miembro (sin quitarlo: membresía persiste)
        with self.rooms_lock:
            room_names = [r.name for r in self.rooms.values() if username in r.members]
        for rn in room_names:
            self.room_broadcast(rn, f"* {username} se desconectó (sigue siendo miembro).", exclude=None)

    # ---------- Parsing / comandos ----------
    def _handle_room_command(self, sender: str, line: str) -> None:
        # Formato esperado:
        # /room [a,b,c] name Amigos status PRIVATE
        rest = line[len("/room"):].strip()
        if not rest:
            self.send_to(sender, "* Uso: /room [nick2,nick3] name <Sala> status <PUBLIC|PRIVATE>")
            return

        # Extraer lista en []
        invitees: List[str] = []
        if "[" in rest and "]" in rest:
            lb = rest.find("[")
            rb = rest.find("]", lb + 1)
            bracket = rest[lb: rb + 1]
            invitees = self._parse_bracket_list(bracket)
            rest2 = (rest[:lb] + rest[rb + 1:]).strip()
        else:
            rest2 = rest

        # Buscar name y status (orden fijo: name X status Y)
        m = re.search(r"\bname\s+(\S+)\s+status\s+(PUBLIC|PRIVATE)\b", rest2, re.IGNORECASE)
        if not m:
            self.send_to(sender, "* Uso: /room [nick2,nick3] name <Sala> status <PUBLIC|PRIVATE>")
            return

        room_name = m.group(1).strip()
        status = m.group(2).upper()

        msg = self._create_room(sender, room_name, status, invitees)
        self.send_to(sender, msg)

    def _handle_invite_command(self, sender: str, line: str) -> None:
        # Acepta:
        # /invite [a,b] room Amigos
        # /invite nick room Amigos
        # /invite [a,b]            (usa sala activa)
        rest = line[len("/invite"):].strip()
        if not rest:
            self.send_to(sender, "* Uso: /invite [nick] room <Sala>  (o sin room usa tu sala activa)")
            return

        invitees: List[str] = []
        room_name: Optional[str] = None

        # Caso lista []
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

        # room opcional
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

        # si su sala activa era esa, limpiar
        if self._get_active_room(sender) == room_name:
            with self.user_ctx_lock:
                self.user_active_room.pop(sender, None)

    def handle_message(self, sender: str, line: str) -> None:
        line = line.strip()
        if not line:
            return

        # Comandos
        if line.startswith("/"):
            cmd = line.split(maxsplit=1)[0].lower()

            if cmd == "/who":
                self.send_to(sender, self._format_rooms_for_who(sender))
                return

            if cmd == "/help":
                self.send_to(sender, "* Comandos: /who, /msg <user> <msg>, /all <msg>, /room [...], /invite [...], /intro <Sala>, /quitroom <Sala>, /help")
                self.send_to(sender, "* DM: @usuario mensaje   | Sala: @Sala mensaje")
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

            # Prioridad: si hay usuario conectado con ese nombre => DM
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

            # Si no es usuario, intentar sala
            with self.rooms_lock:
                room = self.rooms.get(target)

            if not room:
                self.send_to(sender, f"* No existe usuario conectado ni sala llamada '{target}'.")
                return

            # Ver membresía / acceso
            if room.status == "PRIVATE" and sender not in room.members:
                self.send_to(sender, f"* La sala '{target}' es PRIVATE y no eres miembro.")
                return

            # En PUBLIC, si no eres miembro todavía, exige /intro (para que sea claro)
            if room.status == "PUBLIC" and sender not in room.members:
                self.send_to(sender, f"* No estás en '{target}'. Entra con: /intro {target}")
                return

            # Enviar a sala
            self._set_active_room(sender, target)
            formatted = f"[Room:{target}] {sender}: {msg}"
            # echo al emisor
            self.send_to(sender, formatted)
            # a los demás conectados del room
            self.room_broadcast(target, formatted, exclude=sender)
            return

        # Broadcast global por defecto
        self.broadcast(f"[{sender}] {line}", exclude=None)

    # ---------- Manejo de cliente ----------
    def client_reader(self, sock: socket.socket, addr: Tuple[str, int]) -> None:
        buffer = bytearray()
        username: Optional[str] = None
        alive = threading.Event()
        alive.set()
        out_q: "queue.Queue[bytes]" = queue.Queue()

        try:
            sock.settimeout(30.0)
            sock.sendall(self._encode_line("* Bienvenido. Escribe: NICK tu_nombre"))
            # Handshake NICK
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

                    # Ya registrado: arrancamos writer
                    writer_t = threading.Thread(target=self.client_writer, args=(client,), daemon=True)
                    writer_t.start()

                    # Mensajes iniciales
                    self.send_to(username, f"* Conectado como '{username}'. Escribe /help para comandos.")
                    self.broadcast(f"* {username} se unió al chat.", exclude=username)

                    # Si ya pertenece a salas (por invitación offline), avísale
                    with self.rooms_lock:
                        my_rooms = [r.name for r in self.rooms.values() if username in r.members]
                    if my_rooms:
                        self.send_to(username, f"* Ya eres miembro de: {', '.join(sorted(my_rooms))}")
                        # opcional: set active al primero
                        self._set_active_room(username, sorted(my_rooms)[0])

                    break
                else:
                    sock.sendall(self._encode_line("* Primero define tu nombre: NICK tu_nombre"))
                    continue

            if username is None:
                return

            # Loop normal
            sock.settimeout(1.0)
            while alive.is_set() and not self.stop_event.is_set():
                try:
                    line = self.recv_line(sock, buffer)
                except socket.timeout:
                    continue
                if line is None:
                    break
                try:
                    self.handle_message(username, line)
                except Exception:
                    self.send_to(username, "* Error procesando tu mensaje.")
        except (OSError, ValueError):
            pass
        finally:
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

            # Cierra clientes
            with self.clients_lock:
                users = list(self.clients.keys())
            for u in users:
                self.disconnect(u, reason="fue desconectado (servidor cerrando)")

            print("Servidor cerrado.")


def main():
    ap = argparse.ArgumentParser(description="Servidor de chat (TCP + threads + rooms)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5050)
    args = ap.parse_args()

    ChatServer(args.host, args.port).serve_forever()


if __name__ == "__main__":
    main()