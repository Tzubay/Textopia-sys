#!/usr/bin/env python3
import argparse
import curses
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_LINE = 8192


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


@dataclass
class Message:
    ts: str
    text: str


@dataclass
class Conversation:
    key: str                  # GLOBAL | DM:bob | ROOM:Amigos | SYSTEM
    title: str                # Global | bob | Amigos | Sistema
    kind: str                 # GLOBAL | DM | ROOM | SYSTEM
    messages: List[Message] = field(default_factory=list)
    unread: int = 0
    visible: bool = True


class ChatClientTUI:
    def __init__(self, host: str, port: int, nick: str):
        self.host = host
        self.port = port
        self.nick = nick

        self.sock: Optional[socket.socket] = None
        self.stop_event = threading.Event()

        self.state_lock = threading.RLock()

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

        self.incoming_ui_queue: "queue.Queue[str]" = queue.Queue()

        self.last_who_refresh = 0.0
        self.scroll_offsets: Dict[str, int] = {}

        self._ensure_conversation("GLOBAL", "Global", "GLOBAL")
        self._ensure_conversation("SYSTEM", "Sistema", "SYSTEM")

    # -------------------------
    # Estado / conversaciones
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

            # Mantener un límite razonable
            if len(conv.messages) > 1000:
                conv.messages = conv.messages[-1000:]

    def _toast(self, text: str, seconds: float = 3.0):
        with self.state_lock:
            self.toast_text = text
            self.toast_until = time.time() + seconds

    def _clear_toast_if_expired(self):
        with self.state_lock:
            if self.toast_text and time.time() > self.toast_until:
                self.toast_text = ""
                self.toast_until = 0.0

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

    def _select_by_index(self, idx: int):
        with self.state_lock:
            if 0 <= idx < len(self.order):
                self.selected_index = idx
                self.conversations[self.order[self.selected_index]].unread = 0

    # -------------------------
    # Red
    # -------------------------
    def connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self.host, self.port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self.sock.sendall(encode_line(f"NICK {self.nick}"))
        self.status_line = f"Conectado a {self.host}:{self.port} como {self.nick}"

    def send_raw(self, text: str):
        if not self.sock:
            return
        try:
            self.sock.sendall(encode_line(text))
        except OSError:
            self.status_line = "Error enviando mensaje."
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
    # Parsing de mensajes del servidor
    # -------------------------
    def process_server_line(self, line: str):
        line = line.rstrip()

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
            # Esto también es metadata (no lo imprimimos en SYSTEM)
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

        # --- 4) Mensaje global [user] msg ---
        m = re.match(r"^\[([^\]]+)\]\s+(.*)$", line)
        if m:
            sender, msg = m.groups()
            self.online_users.add(sender)
            self._append_message("GLOBAL", f"{sender}: {msg}")
            return

        # --- 5) Eventos típicos del servidor que sí quieres ver (pero en GLOBAL) ---
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
            # Esto no lo metemos a SYSTEM (evita ruido)
            return

        # --- 6) Fallback: lo que no se pudo clasificar va a SYSTEM ---
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
                self.process_server_line(line)
        except OSError:
            self.status_line = "Conexión interrumpida."
            self.stop_event.set()

#    def who_refresh_loop(self):
#        while not self.stop_event.is_set():
#            now = time.time()
#            if now - self.last_who_refresh >= 5:
#                self.send_raw("/who")
#                self.last_who_refresh = now
#            time.sleep(1)

    # -------------------------
    # Envío inteligente según chat seleccionado
    # -------------------------
    def send_from_current_conversation(self, text: str):
        text = text.strip()
        if not text:
            return

        # comandos directos
        if text.startswith("/"):
            if text.lower() in ("/quit", "/exit"):
                self.stop_event.set()
                return

            self.send_raw(text)

            # Ayudas visuales locales
            if text.lower().startswith("/room "):
                self._append_message("SYSTEM", f"(tú ejecutaste) {text}")
            elif text.lower().startswith("/invite "):
                self._append_message("SYSTEM", f"(tú ejecutaste) {text}")
            elif text.lower().startswith("/intro "):
                self._append_message("SYSTEM", f"(tú ejecutaste) {text}")
            elif text.lower().startswith("/quitroom "):
                self._append_message("SYSTEM", f"(tú ejecutaste) {text}")
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

        # SYSTEM u otros -> global por defecto
        self.send_raw(text)
        self._append_message("GLOBAL", f"Tú: {text}")

    # -------------------------
    # UI
    # -------------------------
    def draw(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        min_h = 16
        min_w = 70
        if h < min_h or w < min_w:
            msg = f"Terminal demasiado pequeña. Mínimo {min_w}x{min_h}"
            stdscr.addstr(0, 0, msg[: max(1, w - 1)])
            stdscr.refresh()
            return

        sidebar_w = max(26, int(w * 0.28))
        header_h = 3
        input_h = 3
        footer_h = 2
        main_h = h - header_h - input_h - footer_h

        # Bordes
        stdscr.attron(curses.color_pair(1))
        for x in range(w):
            stdscr.addch(header_h - 1, x, " ")
            stdscr.addch(h - input_h - footer_h, x, " ")
            stdscr.addch(h - footer_h, x, " ")
        stdscr.attroff(curses.color_pair(1))

        # Header
        stdscr.attron(curses.color_pair(2))
        title = f" PYCHAT TUI  |  Usuario: {self.nick}  |  Servidor: {self.host}:{self.port} "
        stdscr.addstr(0, 0, title[:w - 1])
        selected = self._selected_conv()
        stdscr.addstr(1, 0, f" Chat actual: {selected.title} [{selected.kind}] ".ljust(w - 1)[:w - 1])
        stdscr.attroff(curses.color_pair(2))

        # Sidebar
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

        # Info rápida sidebar abajo
        info_y = header_h + main_h - 4
        if info_y > y:
            stdscr.attron(curses.color_pair(3))
            stdscr.addstr(info_y, 0, " Online ".ljust(sidebar_w - 1)[: sidebar_w - 1])
            stdscr.attroff(curses.color_pair(3))
            users_preview = ", ".join(sorted(self.online_users)) if self.online_users else "(nadie)"
            stdscr.addstr(info_y + 1, 0, users_preview[: sidebar_w - 1])

            rooms_preview = ", ".join(sorted(self.public_rooms)) if self.public_rooms else "(ninguna)"
            stdscr.addstr(info_y + 2, 0, f"Public: {rooms_preview}"[: sidebar_w - 1])

        # Main messages
        x0 = sidebar_w + 1
        width_main = w - x0 - 1
        conv = self._selected_conv()
        msgs = conv.messages

        # Scroll
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

        # Input
        stdscr.attron(curses.color_pair(3))
        stdscr.addstr(h - input_h - footer_h + 1, 0, " Escribe mensaje ".ljust(w - 1)[: w - 1])
        stdscr.attroff(curses.color_pair(3))

        prompt = "> "
        typed = self.input_buffer
        visible_input = (prompt + typed)[- (w - 2):]
        stdscr.addstr(h - footer_h - 1, 0, visible_input.ljust(w - 1)[: w - 1])

        # Footer
        help_line = "TAB/Shift+TAB o ←/→ cambiar chat | ↑/↓ scroll | Enter enviar | F5 /who | ESC limpiar | /quit salir"
        stdscr.attron(curses.color_pair(2))
        stdscr.addstr(h - footer_h, 0, help_line.ljust(w - 1)[: w - 1])
        stdscr.addstr(h - footer_h + 1, 0, self.status_line.ljust(w - 1)[: w - 1])
        stdscr.attroff(curses.color_pair(2))

        # Toast / notificación
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

            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                self.stop_event.set()
                break

            if ch == -1:
                time.sleep(0.03)
                continue

            # navegación entre chats
            if ch in (curses.KEY_RIGHT, 9):  # TAB
                self._select_next()
                continue
            if ch == curses.KEY_BTAB:
                self._select_prev()
                continue
            if ch == curses.KEY_LEFT:
                self._select_prev()
                continue

            # scroll del historial
            current_key = self._selected_key()
            if ch == curses.KEY_UP:
                self.scroll_offsets[current_key] = min(
                    self.scroll_offsets.get(current_key, 0) + 1,
                    max(0, len(self.conversations[current_key].messages) - 1)
                )
                continue
            if ch == curses.KEY_DOWN:
                self.scroll_offsets[current_key] = max(
                    0,
                    self.scroll_offsets.get(current_key, 0) - 1
                )
                continue

            # F5 refresh /who
            if ch == curses.KEY_F5:
                self.send_raw("/who")
                self.status_line = "Refrescando /who..."
                continue

            # ESC limpia input
            if ch == 27:
                self.input_buffer = ""
                continue

            # backspace
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.input_buffer = self.input_buffer[:-1]
                continue

            # enter
            if ch in (10, 13):
                text = self.input_buffer.strip()
                self.input_buffer = ""
                if text:
                    self.send_from_current_conversation(text)
                continue

            # texto normal
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
        self._append_message("SYSTEM", "Consejo: cambia entre conversaciones con TAB o flechas.")

        rx = threading.Thread(target=self.receiver_loop, daemon=True)
        rx.start()

        try:
            curses.wrapper(self.ui_loop)
        finally:
            self.close()
            rx.join(timeout=1.0)


def main():
    ap = argparse.ArgumentParser(description="Cliente TUI bonito para chat TCP")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--nick", required=True)
    args = ap.parse_args()

    client = ChatClientTUI(args.host, args.port, args.nick)
    client.run()


if __name__ == "__main__":
    main()