#!/usr/bin/env python3
"""
bridge.py — Puente WebSocket → TCP para Textopia
Conecta el navegador (WebSocket) con chat_server.py (TCP)

Uso:
    python3 bridge.py --tcp-host 127.0.0.1 --tcp-port 5050 --ws-port 6060
"""

import argparse
import asyncio
import socket
import threading
import ssl

# ── Intenta importar websockets, si no está lo instala ──────────────────────
try:
    import websockets
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    import websockets


TCP_HOST = "127.0.0.1"
TCP_PORT = 5050
WS_PORT  = 6060


async def handle_ws_client(websocket):
    """Un cliente WebSocket = una conexión TCP al chat server."""
    path = getattr(websocket, 'path', '/')
    print(f"[bridge] Cliente conectado desde {websocket.remote_address}")

    # Abre conexión TCP al servidor de chat
    try:
        reader, writer = await asyncio.open_connection(TCP_HOST, TCP_PORT)
    except ConnectionRefusedError:
        await websocket.send("* ERROR: No se pudo conectar al servidor de chat. ¿Está corriendo?")
        await websocket.close()
        return

    async def tcp_to_ws():
        """Lee del servidor TCP y manda al navegador."""
        try:
            while True:
                data = await reader.readline()
                if not data:
                    break
                line = data.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    await websocket.send(line)
        except Exception:
            pass
        finally:
            await websocket.close()

    async def ws_to_tcp():
        """Lee del navegador y manda al servidor TCP."""
        try:
            async for message in websocket:
                if not message.endswith("\n"):
                    message += "\n"
                writer.write(message.encode("utf-8", errors="replace"))
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    # Correr ambas tareas
    t1 = asyncio.create_task(tcp_to_ws())
    t2 = asyncio.create_task(ws_to_tcp())
    
    # Si el navegador hace F5, una de las dos tareas muere al instante.
    # FIRST_COMPLETED hace que no nos quedemos colgados esperando a la otra.
    await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
    
    # Matar la tarea que haya quedado viva (para destrabar el puente)
    t1.cancel()
    t2.cancel()
    
    # Colgarle el teléfono al servidor TCP para que libere tu Nick
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    print(f"[bridge] Cliente desconectado: {websocket.remote_address}")


async def main():
    ap = argparse.ArgumentParser(description="Puente WebSocket ↔ TCP para Textopia")
    ap.add_argument("--tcp-host", default="127.0.0.1", help="Host del chat server TCP")
    ap.add_argument("--tcp-port", type=int, default=5050, help="Puerto del chat server TCP")
    ap.add_argument("--ws-port",  type=int, default=6060, help="Puerto WebSocket para el navegador")
    args = ap.parse_args()

    global TCP_HOST, TCP_PORT, WS_PORT
    TCP_HOST = args.tcp_host
    TCP_PORT = args.tcp_port
    WS_PORT  = args.ws_port

    print(f"[bridge] Escuchando WebSocket en ws://127.0.0.1:{WS_PORT}")
    print(f"[bridge] Reenviando al chat server en {TCP_HOST}:{TCP_PORT}")

    async with websockets.serve(handle_ws_client, "0.0.0.0", WS_PORT):
        await asyncio.Future()  # corre para siempre


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bridge] Cerrado.")
