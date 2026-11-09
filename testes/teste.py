import logging
from rendezvous_connection import RendezvousClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

c = RendezvousClient("pyp2p.mfcaetano.cc", 8080, "CIC", "samuel", 8081, ttl=300)
c.register()
print("peers (sem mim):", c.discover())
c.unregister()