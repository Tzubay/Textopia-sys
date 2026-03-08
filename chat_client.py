#!/usr/bin/env python3
import argparse
import socket
import sys
import threading
from typing import Optional

MAX_LINE = 8192


def encode_line(text: str) -> bytes:
    return (text.rstrip("\r\n") + "\n").encode("utf-8", errors="replace")


def recv_line(sock: socket.socket, buffer: bytearray) -> Optional[str]:
    """
    Lee hasta '\n'. Devuelve la línea (sin '\n') o None si el socket se cerró.
    """
    while True:
        nl = buffer.find(b"\n")
        if nl != -1:
            line = buffer[:nl]
            del buffer[: nl + 1]
            if len(line) > MAX_LINE:
                # recortamos para no explotar por spam
                line = line[:MAX_LINE]
            return line.decode("utf-8", errors="replace").rstrip("\r")

        chunk = sock.recv(4096)
        if not chunk:
            return None
        buffer.extend(chunk)
        if len(buffer) > MAX_LINE * 2:
            # si el buffer crece demasiado, evitamos abuso
            buffer.clear()
            return "* (cliente) Buffer demasiado grande; se limpió por seguridad."


class ChatClient:
    def __init__(self, host: str, port: int, nick: str):
        self.host = host
        self.port = port
        self.nick = nick

        self.sock: Optional[socket.socket] = None
        self.stop_event = threading.Event()

        # Para evitar que prints simultáneos se mezclen
        self.print_lock = threading.Lock()

    def safe_print(self, text: str) -> None:
        with self.print_lock:
            print(text, flush=True)

    def connect(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self.host, self.port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s

        # Handshake: mandar NICK
        self.sock.sendall(encode_line(f"NICK {self.nick}"))

    def receiver_loop(self) -> None:
        assert self.sock is not None
        buf = bytearray()

        try:
            while not self.stop_event.is_set():
                line = recv_line(self.sock, buf)
                if line is None:
                    self.safe_print("\n* (cliente) Conexión cerrada por el servidor.")
                    break
                # Mostramos lo recibido
                self.safe_print(line)
        except (OSError, ConnectionResetError, ConnectionAbortedError):
            self.safe_print("\n* (cliente) Error de red: Se perdió la conexión con el servidor.")
            # socket cerrado desde el main
            pass
        finally:
            self.stop_event.set()

    def close(self) -> None:
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

    def run(self) -> None:
        self.connect()
        self.safe_print(f"* Conectado a {self.host}:{self.port} como '{self.nick}'.")
        self.safe_print("* Escribe /help para ver comandos del servidor.")
        self.safe_print("* Comandos locales: /quit, /exit, /clear")

        t = threading.Thread(target=self.receiver_loop, daemon=True)
        t.start()

        try:
            while not self.stop_event.is_set():
                try:
                    msg = input("> ")
                except EOFError:
                    msg = "/quit"
                except KeyboardInterrupt:
                    msg = "/quit"

                msg = msg.strip()
                if not msg:
                    continue

                # Comandos locales
                low = msg.lower()
                if low in ("/quit", "/exit"):
                    self.safe_print("* (cliente) Saliendo...")
                    break
                if low == "/clear":
                    # Limpieza simple (terminal ANSI)
                    self.safe_print("\033[2J\033[H")
                    continue

                # Enviar al servidor tal cual (incluye /who, /room, @Usuario, @Sala, etc.)
                if self.sock:
                    try:
                        self.sock.sendall(encode_line(msg))
                    except (OSError, BrokenPipeError, ConnectionResetError):
                        self.safe_print("* (cliente) Error crítico de E/S enviando mensaje. El servidor se desconectó.")
                        break
        finally:
            self.close()
            # Espera breve al receiver (no bloquea si ya murió)
            t.join(timeout=1.0)


def main():
    ap = argparse.ArgumentParser(description="Cliente de chat (TCP + threads)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--nick", default=None)
    args = ap.parse_args()

    nick = args.nick
    if not nick:
        nick = input("Nick: ").strip()

    if not nick:
        print("Nick vacío. Cancelado.", file=sys.stderr)
        sys.exit(1)

    ChatClient(args.host, args.port, nick).run()


if __name__ == "__main__":
    main()