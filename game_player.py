"""
Projeto: Jogo da Forca P2P (PyP2P-Forca)
Grupo: 13
Integrantes:
  - Lucas Galvão Wosiach -190128828
  - Samuel Henrique Fernandes Silva - 252001163 
"""


import hashlib
import logging
import threading
import time
import unicodedata
import uuid
from enum import Enum
from typing import Callable

log = logging.getLogger("Player")      # eventos do protocolo
game_log = logging.getLogger("Game")   # o que o usuário vê na tela

# send(peer_id, msg) -> bool
SendFn = Callable[[str, dict], bool]

# motivo do protocolo -> texto pro user
MOTIVOS = {
    "not_your_turn": "não é a sua vez",
    "letter_already_used": "essa letra já foi usada",
    "invalid_letter": "digite apenas uma letra de a a z",
    "invalid_word": "palavra inválida: só use letras de a a z",
    "game_not_running": "a partida não está em andamento",
    "unknown_game": "partida não encontrada",
    "game_full": "partida cheia",
    "game_running": "a partida já começou",
    "game_finished": "a partida já terminou",
    "already_joined": "você já está nessa partida",
}

class PlayerState(str, Enum):
    IDLE = "IDLE"
    JOINED = "JOINED"
    PLAYING = "PLAYING"


def _limpar(texto: str) -> str:
    # tira os acentos, deixa minúsculo e tira espaço das pontas
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto)
                         if not unicodedata.combining(c))
    return sem_acento.lower().strip()


class GamePlayer:
    def __init__(self, my_id: str, send: SendFn | None = None,
                 peers: Callable[[], list[str]] | None = None,
                 list_timeout: float = 2.0):
        self.my_id = my_id
        self.send = send           
        self.peers = peers            # peers() -> ids dos vizinhos que anunciaram hangman
        self.list_timeout = list_timeout

        # a Condition usa o mesmo lock, o wait solta ele pros handlers poderem rodar
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

        self._games: dict[str, dict] = {}        # último /games: host -> partida
        self._pending_list: dict[str, str] = {}  # msg_id do GAME_LIST -> vizinho
        self._pending_join: dict | None = None   # JOIN enviado, esperando JOIN_OK/ERR
        self._rejoin_hint: dict | None = None    # partida que a gente perdeu por queda do host

        self._reset()

    @property
    def ocupado(self) -> bool:
        # em partida ou com JOIN pendente (o cli usa pra não deixar ser host e jogador junto)
        with self._lock:
            return self.state != PlayerState.IDLE or self._pending_join is not None

    def _reset(self) -> None:
        # volta pra IDLE e limpa tudo da partida
        self.state = PlayerState.IDLE
        self.host_id: str | None = None
        self.game_id: str | None = None
        self.players: list[str] = []
        self.word_length: int | None = None
        self.initial_lives: int | None = None
        self.word_hash: str | None = None
        self.turn_timeout: float | None = None
        self.last_seq = 0                        # último STATE aplicado
        self.masked: str | None = None
        self.wrong_letters: list[str] = []
        self.lives: int | None = None
        self.turn: str | None = None
        self.last_guess: dict | None = None
        self._state_time: float | None = None    # quando o último STATE chegou

    # comandos do cli
    def list_games(self, timeout: float | None = None) -> list[dict]:
        # manda GAME_LIST pra todos os vizinhos e espera as respostas (ou o timeout)
        timeout = self.list_timeout if timeout is None else timeout
        vizinhos = list(self.peers()) if self.peers else []
        with self._lock:
            self._games = {}
            self._pending_list = {}

        for peer in vizinhos:
            msg = self._make("GAME_LIST", peer)
            # registra antes de enviar, a resposta pode chegar rapidíssimo
            with self._lock:
                self._pending_list[msg["msg_id"]] = peer
            if not self._send(msg):
                with self._lock:
                    self._pending_list.pop(msg["msg_id"], None)

        with self._cond:
            self._cond.wait_for(lambda: not self._pending_list, timeout)
            self._pending_list = {}              # quem não respondeu a tempo fica de fora
            return list(self._games.values())

    def join(self, host_id: str) -> None:
        with self._lock:
            if self.state != PlayerState.IDLE or self._pending_join:
                raise ValueError("Você já está em uma partida.")
            jogo = self._games.get(host_id)

        # não viu essa partida no último /games: pergunta de novo
        if jogo is None:
            self.list_games()
            with self._lock:
                jogo = self._games.get(host_id)
        if jogo is None:
            raise ValueError(f"{host_id} não tem partida aberta (veja /games).")

        with self._lock:
            if self.state != PlayerState.IDLE or self._pending_join:
                raise ValueError("Você já está em uma partida.")
            msg = self._make("JOIN", host_id, game_id=jogo["game_id"])
            self._pending_join = {
                "msg_id": msg["msg_id"], "host": host_id, "game_id": jogo["game_id"],
                "dados": {"word_length": jogo.get("word_length"), "lives": jogo.get("lives")},
                "auto": False,
            }
            self._rejoin_hint = None
            if not self._send(msg):
                self._pending_join = None
                raise ValueError(f"Não foi possível enviar JOIN para {host_id}.")
            log.info("JOIN enviado a %s (%s)", host_id, jogo["game_id"])

    def leave(self) -> None:
        with self._lock:
            # também sai de um JOIN que ainda não foi respondido, senão o host fica com um fantasma
            if self.state != PlayerState.IDLE:
                host, gid = self.host_id, self.game_id
            elif self._pending_join:
                host, gid = self._pending_join["host"], self._pending_join["game_id"]
            else:
                raise ValueError("Você não está em uma partida.")

            self._pending_join = None
            self._rejoin_hint = None
            # sai mesmo se o envio falhar
            self._send(self._make("LEAVE", host, game_id=gid))
            self._reset()
            game_log.info("Você saiu da partida %s", gid)

    def guess_letter(self, letter: str) -> None:
        letra = _limpar(letter)
        if len(letra) != 1 or not ("a" <= letra <= "z"):
            raise ValueError("Digite uma única letra de a a z.")
        self._palpitar(letter=letra)

    def guess_word(self, word: str) -> None:
        palavra = _limpar(word)
        if not palavra or not all("a" <= c <= "z" for c in palavra):
            raise ValueError("Palavra inválida: use apenas letras de a a z.")
        self._palpitar(word=palavra)

    def _palpitar(self, **campo) -> None:
        # não confere a vez aqui: quem responde not_your_turn é o host
        with self._lock:
            if self.state != PlayerState.PLAYING:
                raise ValueError("Você não está em uma partida em andamento.")
            msg = self._make("GUESS", self.host_id, game_id=self.game_id, **campo)
            if not self._send(msg):
                raise ValueError("Não foi possível enviar o palpite (host desconectado?).")
            log.info("GUESS enviado %s", campo)

    def shutdown(self) -> None:
        # /quit: avisa o host (LEAVE) antes do BYE
        with self._lock:
            if self.state != PlayerState.IDLE or self._pending_join:
                self.leave()

    def status_lines(self) -> list[str]:
        # linhas do /status (o cli imprime, ou show_status joga no log)
        with self._lock:
            if self.state == PlayerState.IDLE:
                return ["Você não está em nenhuma partida."]

            status = "OPEN" if self.state == PlayerState.JOINED else "RUNNING"
            l1 = f"{self.game_id}  (host: {self.host_id})  status: {status}"

            palavra = " ".join(self.masked) if self.masked else "—"
            vidas = self.lives if self.initial_lives is None else f"{self.lives} / {self.initial_lives}"
            erradas = ", ".join(self.wrong_letters) or "—"
            l2 = f"Palavra: {palavra}     Vidas: {vidas}     Erradas: {erradas}"

            l3 = f"Jogadores: {', '.join(self.players) or '—'}"
            if self.state == PlayerState.JOINED:
                l3 += "   aguardando o host iniciar"
            elif self.turn:
                l3 += f"   Vez de: {self.turn}{' (você)' if self.turn == self.my_id else ''}"
                if self.turn_timeout and self._state_time is not None:
                    restante = max(0, int(self.turn_timeout - (time.monotonic() - self._state_time)))
                    l3 += f"  — {restante} s restantes"
            return [l1, l2, l3]

    def show_status(self) -> None:
        for linha in self.status_lines():
            game_log.info(linha)

    # mensagens recebidas (o router chama)
    def handle_game_list_ok(self, msg: dict) -> None:
        with self._cond:
            # só vale resposta de quem a gente perguntou
            peer = self._pending_list.get(msg["msg_id"])
            if peer is None or peer != msg["src"]:
                log.debug("GAME_LIST_OK inesperada de %s, ignorada", msg["src"])
                return
            del self._pending_list[msg["msg_id"]]

            jogos = msg.get("games")
            for jogo in jogos if isinstance(jogos, list) else []:
                # o vizinho só pode anunciar a própria partida
                if (isinstance(jogo, dict) and jogo.get("host") == peer
                        and isinstance(jogo.get("game_id"), str)):
                    self._games[peer] = jogo
            self._cond.notify_all()

    def handle_join_ok(self, msg: dict) -> None:
        with self._lock:
            p = self._pending_join
            if (p is None or msg["msg_id"] != p["msg_id"] or msg["src"] != p["host"]
                    or msg.get("game_id") != p["game_id"]):
                log.debug("JOIN_OK inesperado de %s, ignorado", msg["src"])
                return
            self._pending_join = None
            dados = p["dados"]

            self._reset()
            self.state = PlayerState.JOINED
            self.host_id, self.game_id = p["host"], p["game_id"]
            self.players = list(msg["players"]) if isinstance(msg.get("players"), list) else []
            self.word_length = dados.get("word_length")
            self.initial_lives = dados.get("lives")
            self.lives = self.initial_lives
            self.word_hash = dados.get("word_hash")
            self.turn_timeout = dados.get("turn_timeout")
            if isinstance(self.word_length, int):
                self.masked = "_" * self.word_length

            log.info("JOIN_OK %s players=%s rejoin=%s", self.game_id, self.players,
                     msg.get("rejoin"))
            game_log.info("Entrou na partida %s (host: %s)", self.game_id, self.host_id)

    def handle_join_err(self, msg: dict) -> None:
        with self._lock:
            p = self._pending_join
            if p is None or msg["msg_id"] != p["msg_id"] or msg["src"] != p["host"]:
                log.debug("JOIN_ERR inesperado de %s, ignorado", msg["src"])
                return
            self._pending_join = None
            motivo = str(msg.get("reason"))
            if p["auto"]:
                log.debug("rejoin automático recusado (%s)", motivo)
            else:
                game_log.warning("Entrada recusada por %s: %s", p["host"],
                                 MOTIVOS.get(motivo, motivo))

    def handle_leave_ok(self, msg: dict) -> None:
        # a gente já voltou pra IDLE quando mandou o LEAVE
        log.debug("LEAVE_OK de %s", msg["src"])

    def handle_game_start(self, msg: dict) -> None:
        with self._lock:
            if not self._da_partida(msg) or self.state != PlayerState.JOINED:
                log.debug("GAME_START ignorado")
                return
            players, lives, tamanho = msg.get("players"), msg.get("lives"), msg.get("word_length")
            if not (isinstance(players, list) and isinstance(lives, int)
                    and isinstance(tamanho, int) and isinstance(msg.get("word_hash"), str)):
                log.warning("GAME_START malformado de %s, ignorado", msg["src"])
                return

            self.players = list(players)
            self.initial_lives = self.lives = lives
            self.word_length = tamanho
            self.masked = "_" * tamanho
            self.word_hash = msg["word_hash"]
            self.turn_timeout = msg.get("turn_timeout")
            self.last_seq = 0
            self.state = PlayerState.PLAYING
            log.info("GAME_START players=%s lives=%d word_length=%d", self.players, lives, tamanho)
            game_log.info("A partida começou! Jogadores: %s", ", ".join(self.players))

    def handle_state(self, msg: dict) -> None:
        with self._lock:
            if not self._da_partida(msg):
                log.debug("STATE ignorado (não é do host da partida atual)")
                return

            seq = msg.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool):
                log.warning("STATE sem seq válido, ignorado")
                return
            # fora de ordem ou repetido: descarta
            if seq <= self.last_seq:
                log.debug("STATE seq=%d ignored (already at seq=%d)", seq, self.last_seq)
                return

            masked, errou, vidas = msg.get("masked"), msg.get("wrong_letters"), msg.get("lives")
            turno, jogadores = msg.get("turn"), msg.get("players")
            if not (isinstance(masked, str) and isinstance(errou, list) and isinstance(vidas, int)
                    and isinstance(jogadores, list) and (turno is None or isinstance(turno, str))):
                log.warning("STATE seq=%d malformado, ignorado", seq)
                return

            # um STATE em JOINED é rejoin (não teve GAME_START pra esse processo)
            self.state = PlayerState.PLAYING
            self.last_seq = seq
            self.masked = masked
            self.wrong_letters = [str(x) for x in errou]
            self.lives = vidas
            self.turn = turno
            self.players = [str(x) for x in jogadores]
            last = msg.get("last_guess")
            self.last_guess = last if isinstance(last, dict) else None
            self._state_time = time.monotonic()

            sou_eu = turno == self.my_id
            log.info("STATE seq=%d applied: %s lives=%d turn=%s%s", seq, masked, vidas,
                     turno, " (você)" if sou_eu else "")
            texto = self._texto_jogada(self.last_guess)
            if texto:
                game_log.info(texto)
            if sou_eu:
                game_log.info("É a sua vez! Use /guess <letra> ou /word <palavra>")

    def handle_guess_result(self, msg: dict) -> None:
        with self._lock:
            if not self._da_partida(msg):
                return
            if msg.get("accepted") is False:
                motivo = str(msg.get("reason"))
                log.info("GUESS_RESULT recusado (%s)", motivo)
                game_log.warning("Jogada recusada: %s", MOTIVOS.get(motivo, motivo))
            else:
                # o STATE que vem junto mostra o resultado
                log.info("GUESS_RESULT aceito correct=%s", msg.get("correct"))

    def handle_game_over(self, msg: dict) -> None:
        with self._lock:
            if not self._da_partida(msg):
                log.debug("GAME_OVER ignorado")
                return
            resultado, palavra = msg.get("result"), msg.get("word")

            # confere o hash que veio no GAME_START
            if self.word_hash is None or not isinstance(palavra, str):
                hash_txt = "hash não verificado"
            elif hashlib.sha256(palavra.encode("utf-8")).hexdigest() == self.word_hash.lower():
                hash_txt = "hash OK"
            else:
                log.warning("host cheated: hash mismatch")
                hash_txt = "hash DIVERGENTE"

            if resultado == "ABORTED":
                log.info("GAME_OVER ABORTED reason=%s", msg.get("reason"))
                game_log.info("Partida encerrada (%s). A palavra era '%s'.",
                              msg.get("reason"), palavra)
            else:
                log.info("GAME_OVER %s word=%s winner=%s — %s", resultado, palavra,
                         msg.get("winner"), hash_txt)
                if resultado == "WON":
                    game_log.info("Ganhou! A palavra era '%s' (quem completou: %s).",
                                  palavra, msg.get("winner"))
                else:
                    game_log.info("Perdeu! A palavra era '%s'.", palavra)
            scores = msg.get("scores")
            if isinstance(scores, dict) and scores:
                game_log.info("Placar: %s", ", ".join(f"{k}: {v}" for k, v in scores.items()))

            self._rejoin_hint = None
            self._reset()

    # eventos de conexão (o p2p_client chama)
    def on_peer_lost(self, peer_id: str) -> None:
        with self._lock:
            # quem estava respondendo o /games e caiu não precisa esperar o timeout
            for mid, peer in list(self._pending_list.items()):
                if peer == peer_id:
                    del self._pending_list[mid]
            self._cond.notify_all()

            if self._pending_join and self._pending_join["host"] == peer_id:
                self._pending_join = None

            if self.state != PlayerState.IDLE and peer_id == self.host_id:
                game_log.warning("Host %s unreachable — game aborted", peer_id)
                # guarda a partida: se a conexão voltar, reenvia o JOIN (vira rejoin no host)
                if self.state == PlayerState.PLAYING:
                    self._rejoin_hint = {
                        "host": self.host_id, "game_id": self.game_id,
                        "dados": {"word_length": self.word_length, "lives": self.initial_lives,
                                  "word_hash": self.word_hash, "turn_timeout": self.turn_timeout},
                    }
                self._reset()

    def on_peer_connected(self, peer_id: str) -> None:
        with self._lock:
            h = self._rejoin_hint
            if h is None or h["host"] != peer_id or self.state != PlayerState.IDLE or self._pending_join:
                return
            self._rejoin_hint = None
            msg = self._make("JOIN", peer_id, game_id=h["game_id"])
            self._pending_join = {"msg_id": msg["msg_id"], "host": peer_id,
                                  "game_id": h["game_id"], "dados": h["dados"], "auto": True}
            if self._send(msg):
                log.info("reconectado a %s, reenviando JOIN (%s)", peer_id, h["game_id"])
            else:
                self._pending_join = None

    # auxiliares
    def _da_partida(self, msg: dict) -> bool:
        # só aceita mensagem do host da partida atual
        return (self.state != PlayerState.IDLE and msg.get("src") == self.host_id
                and msg.get("game_id") == self.game_id)

    def _texto_jogada(self, lg: dict | None) -> str | None:
        if not lg:
            return None
        quem = lg.get("player")
        if lg.get("timeout"):
            return f"{quem} perdeu a vez (timeout)."
        resultado = "acertou" if lg.get("correct") else "errou"
        if "letter" in lg:
            return f"{quem} jogou '{lg['letter']}': {resultado}."
        if "word" in lg:
            return f"{quem} chutou '{lg['word']}': {resultado}."
        return None

    def _make(self, type: str, dst: str, **fields) -> dict:
        return {"type": type, "msg_id": str(uuid.uuid4()), "ttl": 1,
                "src": self.my_id, "dst": dst, **fields}

    def _send(self, msg: dict) -> bool:
        # falha de envio vira False, nunca exceção
        try:
            ok = self.send(msg["dst"], msg)
        except Exception:
            log.exception("Erro ao enviar %s a %s", msg["type"], msg["dst"])
            return False
        if not ok:
            log.warning("Falha ao enviar %s a %s", msg["type"], msg["dst"])
        return bool(ok)