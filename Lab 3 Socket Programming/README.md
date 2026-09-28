# CSC-334: Lab 03 — Socket Programming with Multi-Threading

| | |
|---|---|
| **Name** | Gulfam Ali |
| **Registration No.** | FA23-BSE-030 |
| **Course** | CSC-334 Parallel and Distributed Computing |

This folder contains the solution for **Lab 03: Socket Programming with Multi-Threading** (Parallel and Distributed Computing, CSC-334).

## 📁 Files

| File | Description |
|---|---|
| `server.py` | Multi-threaded TCP server. Accepts multiple clients at once, spawns a new thread per client, and prints the active thread name, client IP, and port. |
| `client.py` | TCP client. Connects to the server and keeps exchanging messages until the user types `exit`. |

## ▶️ How to Run

1. Make sure Python 3 is installed.
2. Open one terminal and start the server:
   ```
   python server.py
   ```
3. Open one or more additional terminals (one per client) and run:
   ```
   python client.py
   ```
4. Type messages in the client terminal. The server will echo back a response.
5. Type `exit` in a client terminal to close that connection.

Because the server uses a new thread for every client, you can open several client terminals at the same time and the server will handle all of them simultaneously.

---

## 📚 Concepts Used in This Lab

### What is a socket?
A **socket** is an endpoint for sending and receiving data across a network. Think of it like a phone — one program "calls" (connects to) another program's socket, and once connected, both sides can send and receive messages through it. In Python, sockets are created using the `socket` module and are used to build both the server (which listens for connections) and the client (which connects to the server).

### What is a thread?
A **thread** is the smallest unit of execution within a program. It is a lightweight sub-process that shares the same memory space as the main program but can run independently. Threads are cheaper to create than full processes because they don't need their own separate memory — they just need a small amount of overhead to track their own execution.

### What is multithreading?
**Multithreading** means running multiple threads at the same time within a single program (process). Instead of handling one task and then moving to the next, the program can handle several tasks "in parallel" (or interleaved, depending on the CPU). In this lab, multithreading lets the server talk to many clients at once instead of forcing clients to wait in a queue.

### Difference between a process and a thread
| Process | Thread |
|---|---|
| An independent program with its own memory space | A unit of execution *inside* a process, sharing that process's memory |
| Heavier — more memory and CPU overhead to create | Lightweight — cheaper and faster to create |
| Processes don't share memory with each other directly | Threads within the same process share memory and resources |
| Crash in one process usually doesn't affect another | An unhandled crash in one thread can affect the whole process |

In short: a **process** is like a separate house, while **threads** are like different people living and working inside the same house, sharing the same rooms (memory).

### Why use multithreading in a server?
A normal (single-threaded) server can only talk to **one client at a time** — every other client has to wait until the first one is done. By giving each client its own thread, the server can:
- Accept and serve **multiple clients simultaneously**.
- Keep the program responsive instead of blocking on one slow client.
- Make better use of the CPU by handling I/O-bound tasks (like waiting for network data) concurrently.

This is exactly what `server.py` does: every time a new client connects, `threading.Thread()` spins up a dedicated thread just for that client.

### `_thread` vs `threading`
- **`_thread`** is Python's low-level, older module for working with threads. It gives you basic thread creation but very few extra features or safety tools, and it's generally discouraged for everyday use.
- **`threading`** is the higher-level, modern module built on top of `_thread`. It provides an easier, object-oriented API (`Thread` objects), plus useful synchronization tools like `Lock`, `RLock`, `Event`, and `Semaphore`. This is the module used in this lab (`import threading`) because it's safer and easier to work with.

### What is a lock?
A **lock** (`threading.Lock()`) is a synchronization tool used to make sure that **only one thread at a time** can access a shared resource — like a shared variable, a file, or (in this lab) the console output. Without a lock, multiple threads printing to the terminal at the same time can cause messy, overlapping, or corrupted output. A lock prevents this by letting only one thread "hold" it at a time; every other thread must wait until it's released.

### What do `acquire()` and `release()` do?
These are the two core methods of a `Lock` object:
- **`lock.acquire()`** — puts the lock into the **"locked"** state. If another thread already holds the lock, the calling thread will **wait (block)** until it becomes free.
- **`lock.release()`** — puts the lock back into the **"unlocked"** state, allowing another waiting thread to acquire it.

In `server.py`, this is simplified using:
```python
with print_lock:
    print(...)
```
The `with` statement automatically calls `acquire()` before the block and `release()` after it, even if an error occurs — so the shared print statements never overlap between threads.

### What does `Thread()` do?
`threading.Thread()` creates a **new thread object** that can run a function independently of the main program. It takes (at least) two useful arguments:
- `target` — the function you want this thread to run (e.g., `handle_client`).
- `args` — a tuple of arguments to pass to that function (e.g., the client's connection object and address).

Calling `.start()` on the thread object actually begins running that function in its own thread — this is what allows the server to immediately go back to `accept()`-ing new clients while previously connected clients are handled independently in the background.

```python
thread = threading.Thread(target=handle_client, args=(conn, addr))
thread.start()
```

---

## 🖼️ Screenshots

Server and client terminal output showing multiple clients connected and exchanging messages:

![Screenshot 1](<Screenshot 2026-09-27 230838-1.png>)
![Screenshot 2](<Screenshot 2026-09-27 230838.png>)
![Screenshot 3](<Screenshot 2026-09-27 230948.png>)
![Screenshot 4](<Screenshot 2026-09-27 231040.png>)
![Screenshot 5](<Screenshot 2026-09-27 231047.png>)
