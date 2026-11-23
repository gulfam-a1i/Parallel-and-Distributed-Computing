"""
CSC-334 : Parallel and Distributed Computing
Lab 03 : Socket Programming with Multi-Threading
Client Script
"""

import socket

HOST = '127.0.0.1'   # server's IP (localhost)
PORT = 5000           # server's port


def main():
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect((HOST, PORT))

    welcome = client.recv(1024).decode()
    print(welcome)

    while True:
        message = input("You: ")
        client.send(message.encode())

        if message.strip().lower() == 'exit':
            print("[DISCONNECTED] You left the chat.")
            break

        response = client.recv(1024).decode()
        print(f"Server: {response}")

    client.close()


if __name__ == "__main__":
    main()
