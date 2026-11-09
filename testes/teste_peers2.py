import logging, time
from peer_connection import PeerServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

a  = PeerServer("a@T", 9001, lambda c, m: None)
b  = PeerServer("b@T", 9002, lambda c, m: None)
b2 = PeerServer("b@T", 9003, lambda c, m: None)   # "b reiniciado": mesmo peer_id
for s in (a, b, b2):
    s.start()

b.conectar("127.0.0.1", 9001)
time.sleep(1)
print("antes :", [(c.peer_id, c.direcao) for c in a.conexoes()])
b2.conectar("127.0.0.1", 9001)
time.sleep(3)
print("depois:", [(c.peer_id, c.direcao, c.endereco) for c in a.conexoes()])
a.encerrar(); b.encerrar(); b2.encerrar()