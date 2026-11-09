import logging, time
from peer_connection import PeerServer
from keep_alive import KeepAlive

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

surdo = {"b": False}

def msg_a(conn, msg):
    ka_a.tratar(conn, msg)

def msg_b(conn, msg):
    if surdo["b"] and msg["type"] == "PING":
        return                      # b "trava": para de responder PONG
    ka_b.tratar(conn, msg)

a = PeerServer("a@T", 9001, msg_a)
b = PeerServer("b@T", 9002, msg_b)
ka_a = KeepAlive(a, ping_interval=1)
ka_b = KeepAlive(b, ping_interval=1)
a.start(); b.start(); ka_a.start(); ka_b.start()

a.conectar("127.0.0.1", 9002)
time.sleep(5)
print(">>> b para de responder PONG")
surdo["b"] = True
time.sleep(7)
print("conexões de a:", [c.peer_id for c in a.conexoes()])
a.encerrar(); b.encerrar()