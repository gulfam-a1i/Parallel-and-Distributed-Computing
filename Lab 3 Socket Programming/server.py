"""
CSC-334 : Parallel and Distributed Computing
Lab 03 : Socket Programming with Multi-Threading
Multi-Threaded TCP Server
"""

import socket
import threading

HOST = '127.0.0.1'   # localhost
PORT = 5000           # port to listen on

# Lock for synchronizing access to shared resources (e.g. print statements)
print_lock = threading.Lock()


def handle_client(conn, addr):
    """Handles communication with a single connected client."""
    thread_name = threading.current_thread().name

    with print_lock:
        print(f"[NEW CONNECTION] {thread_name} handling Client {addr[0]}:{addr[1]}")

    conn.send("Connected to the multi-threaded server. Type 'exit' to quit.".encode())

    while True:
        try:
            data = conn.recv(1024).decode()
            if not data or data.strip().lower() == 'exit':
                with print_lock:
                    print(f"[DISCONNECTED] {thread_name} closing connection with {addr[0]}:{addr[1]}")
                break

            with print_lock:
                print(f"[{thread_name}] Received from {addr[0]}:{addr[1]} -> {data}")

            reply = f"Server ({thread_name}) received: {data}"
            conn.send(reply.encode())

        except ConnectionResetError:
            with print_lock:
                print(f"[ERROR] {thread_name} lost connection with {addr[0]}:{addr[1]}")
            break

    conn.close()


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()

    print(f"[STARTING] Server is starting on {HOST}:{PORT}")
    print("[LISTENING] Server is listening for connections...")

    while True:
        conn, addr = server.accept()
        thread = threading.Thread(target=handle_client, args=(conn, addr))
        thread.start()
        with print_lock:
            print(f"[ACTIVE CONNECTIONS] {threading.active_count() - 1}")


if __name__ == "__main__":
    main()
