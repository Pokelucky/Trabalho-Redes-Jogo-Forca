"""
Projeto: Jogo da Forca P2P (PyP2P-Forca)
Grupo: 13
Integrantes:
  - Lucas Galvão Wosiach -190128828
  - Samuel Henrique Fernandes Silva - 252001163 
"""


import logging
import threading

from cli import Cli
from game_host import GameHost
from game_player import GamePlayer
from keep_alive import KeepAlive
from message_router import MessageRouter
from peer_connection import PeerServer
from peer_table import PeerTable
from rendezvous_connection import RendezvousWorker
from rendezvous_connection import RendezvousClient

log = logging.getLogger("P2P")


class P2PForcaApp:
    """Monta os módulos, liga os callbacks e encerra na ordem da spec."""

    def __init__(self, cfg: dict):
        self.meu_id = f"{cfg['name']}@{cfg['namespace']}"
        self._encerrando = threading.Event()

        self.rdv = RendezvousClient(cfg["rdv_host"], cfg["rdv_port"], cfg["namespace"],
                                    cfg["name"], cfg["listen_port"], cfg["register_ttl"])
        self.host = GameHost(self.meu_id, None, lives=cfg["lives"],
                             turn_timeout=cfg["turn_timeout"])
        self.player = GamePlayer(self.meu_id, None, peers=self._vizinhos_hangman)
        self.router = MessageRouter(self.host, self.player, None)
        self.server = PeerServer(self.meu_id, cfg["listen_port"], self.router.handle,
                                 self._on_connect, self._on_disconnect)
        self.ka = KeepAlive(self.server, cfg["ping_interval"], cfg["ping_miss_limit"])
        self.router.ka = self.ka
        self.host.send = self.server.enviar
        self.player.send = self.server.enviar

        self.table = PeerTable(self.server, base_backoff=cfg["base_backoff"],
                               max_backoff=cfg["max_backoff"],
                               max_attempts=cfg["max_reconnect_attempts"],
                               meu_ip=lambda: self.rdv.meu_ip)
        self.worker = RendezvousWorker(self.rdv, cfg["discover_interval"],
                                       self.table.reconciliar)
        self.cli = Cli(self.host, self.player, self.server, self.ka, self.table,
                       self.rdv, self.worker, on_quit=self.encerrar,
                       words_file=cfg["words_file"],
                       default_max_players=cfg["max_players"])

    # ---------- callbacks do PeerServer (um de cada, então chamam todo mundo) ----------
    def _vizinhos_hangman(self):
        return [c.peer_id for c in self.server.conexoes() if c.suporta_forca]

    def _on_connect(self, conn):
        self._seguro(self.table.on_connect, conn)
        self._seguro(self.player.on_peer_connected, conn.peer_id)

    def _on_disconnect(self, conn):
        self._seguro(self.table.on_disconnect, conn)
        self._seguro(self.host.on_peer_lost, conn.peer_id)
        self._seguro(self.player.on_peer_lost, conn.peer_id)

    @staticmethod
    def _seguro(fn, *args):
        try:
            fn(*args)
        except Exception:
            log.exception("erro em %s", getattr(fn, "__name__", fn))

    # ---------- ciclo de vida ----------
    def iniciar(self):
        self.server.start()      # escuta antes de se registrar
        self.ka.start()
        self.table.start()
        self.worker.start()      # REGISTER + DISCOVER periódico

    def encerrar(self):
        """/quit: GAME_OVER ou LEAVE -> BYE/BYE_OK -> UNREGISTER (nessa ordem)."""
        if self._encerrando.is_set():
            return
        self._encerrando.set()
        self._seguro(self.host.shutdown)       # host: GAME_OVER ABORTED
        self._seguro(self.player.shutdown)     # jogador: LEAVE
        self.worker.parar()                    # senão ele registraria de novo
        self.table.parar()
        self.ka.parar()
        self._seguro(self.server.encerrar, "shutdown")   # BYE a todos
        if self.worker.is_alive():
            self.worker.join(timeout=6)
        try:
            self.rdv.unregister()
        except Exception as e:
            log.warning("unregister falhou: %s", e)