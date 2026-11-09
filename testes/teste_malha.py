import logging
import sys
import time

from rendezvous_connection import RendezvousClient
from rdv_worker_snippet import RendezvousWorker
from peer_connection import PeerServer
from keep_alive import KeepAlive
from peer_table import PeerTable

nome, porta = sys.argv[1], int(sys.argv[2])
NS = "TESTE-SAMUEL"
logging.basicConfig(level=logging.INFO,
                    format=f"%(asctime)s {nome} [%(name)s] %(levelname)s %(message)s")

cliente = RendezvousClient("pyp2p.mfcaetano.cc", 8080, NS, nome, porta, ttl=60)
server = PeerServer(f"{nome}@{NS}", porta,
                    lambda c, m: ka.tratar(c, m) if m["type"] in ("PING", "PONG") else None)
ka = KeepAlive(server, ping_interval=5)
tabela = PeerTable(server, base_backoff=2, max_backoff=20, max_attempts=4,
                   meu_ip=lambda: cliente.meu_ip)
server.on_connect = tabela.on_connect
server.on_disconnect = tabela.on_disconnect
worker = RendezvousWorker(cliente, 10, tabela.reconciliar)

server.start(); ka.start(); tabela.start(); worker.start()
try:
    while True:
        time.sleep(10)
        print(f"--- {nome} ---")
        for linha in tabela.listar(ka):
            print(linha)
except KeyboardInterrupt:
    pass
finally:
    worker.parar(); tabela.parar(); ka.parar()
    server.encerrar()
    try:
        cliente.unregister()
    except Exception as e:
        print("unregister falhou:", e)