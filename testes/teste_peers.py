import logging, threading, time
from peer_connection import PeerServer, criar_msg

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

def cb(nome):
    return lambda conn, msg: print(f"[{nome}] recebeu de {conn.peer_id}: {msg['type']}")

a = PeerServer("a@T", 9001, cb("a"))
b = PeerServer("b@T", 9002, cb("b"))
a.start(); b.start()

# conexões simultâneas, para forçar a duplicada
ta = threading.Thread(target=a.conectar, args=("127.0.0.1", 9002))
tb = threading.Thread(target=b.conectar, args=("127.0.0.1", 9001))
ta.start(); tb.start(); ta.join(); tb.join()
time.sleep(3)

for nome, s in (("a", a), ("b", b)):
    print(nome, [(c.peer_id, c.direcao) for c in s.conexoes()])

a.enviar("b@T", criar_msg("GAME_LIST", src="a@T", dst="b@T"))
time.sleep(1)
a.encerrar(); b.encerrar()