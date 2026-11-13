import logging
import threading
import time
import uuid
from enum import Enum
from typing import Callable

from hangman import Hangman

logger = logging.getLogger("Host")   # a spec e a avaliação usam o rótulo [Host]

# retorna True se enviou com sucesso
SendFn = Callable[[str, dict], bool]

class HostState(str, Enum):
    IDLE = "IDLE"
    OPEN = "OPEN"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


class GameHost:
    def __init__(
        self,
        my_id: str,
        send: SendFn | None,
        lives: int = 6,
        turn_timeout: float = 30,
        finished_ttl: float = 60,
    ):
        """
        my_id:         peer_id deste peer (ex: alice@CIC)
        send:          send(peer_id, msg) -> bool - função de envio, pode ser definida depois da criação
        lives:         vidas iniciais (config.json).
        turn_timeout:  tempo limite por turno (config.json).
        finished_ttl:  tempo até uma partida encerrada voltar a IDLE
        """
        self.my_id = my_id
        self.send = send
        self.lives = lives
        self.turn_timeout = turn_timeout
        self.finished_ttl = finished_ttl

        self._lock = threading.RLock()

        self.state: HostState = HostState.IDLE
        self.game_id: str | None = None
        self.hangman: Hangman | None = None   # criado no create(), iniciado no start()
        self.max_players: int = 0
        self.lobby_players: list[str] = []    # inscritos antes do start (ordem de entrada)
        self.seq: int = 0                     # número de sequência do STATE

        # timers (threading.Timer) e seus tokens
        self._turn_timer: threading.Timer | None = None
        self._turn_id: int = 0
        self._idle_timer: threading.Timer | None = None
        self._idle_id: int = 0
        # tempo em que o turno atual expira, usado pelo /status.
        self._turn_deadline: float | None = None

    # ------------------------------------------------------------------
    # comandos locais (chamados pelo CLI)
    # ------------------------------------------------------------------
    def create(self, word: str, max_players: int) -> str:
        """/create: IDLE (ou FINISHED) -> OPEN. Devolve o game_id.

        Levanta ValueError se já tem partida OPEN/RUNNING, se max_players < 1 ou se a
        palavra é inválida. tudo é validado antes de qualquer atribuição ao self.
        Decisão: criar a partir de FINISHED é permitido (a partida antiga deixa de
        aparecer no GAME_LIST_OK), para o host não precisar esperar os 60s
        """
        with self._lock:
            if self.state not in (HostState.IDLE, HostState.FINISHED):
                raise ValueError("Você já tem uma partida em andamento ou aberta.")
            if max_players < 1:
                raise ValueError("max_players deve ser pelo menos 1.")

            hangman = Hangman(word, self.lives)               # ValueError se a palavra é inválida
            game_id = f"{self.my_id}/{uuid.uuid4().hex[:8]}"

            if self.state == HostState.FINISHED:
                self._reset_to_idle()

            self.hangman = hangman
            self.game_id = game_id
            self.max_players = max_players
            self.lobby_players = []
            self.seq = 0
            self.state = HostState.OPEN
            logger.info("Game %s created (word_length=%d, lives=%d)",
                        game_id, len(hangman.word), self.lives)
            return game_id

    def start(self) -> None:
        # /start: OPEN -> RUNNING. ValueError se n tem partida OPEN ou o lobby ta vazio
        with self._lock:
            if self.state != HostState.OPEN:
                raise ValueError("Não há partida aberta para iniciar.")
            if not self.lobby_players:
                raise ValueError("É necessário pelo menos um jogador inscrito.")

            self.hangman.start(self.lobby_players)
            self.state = HostState.RUNNING
            self.seq = 0

            self._broadcast(
                "GAME_START",
                game_id=self.game_id,
                players=list(self.hangman.players),
                lives=self.lives,
                word_length=len(self.hangman.word),
                word_hash=self.hangman.word_hash,
                turn_timeout=self.turn_timeout,
            )
            self._send_state(None)                            # seq=1
            self._start_turn_timer()

    def cancel(self) -> None:
        # /cancel: OPEN ou RUNNING -> ABORTED
        # encerra uma partida aberta ou em andamento

        with self._lock:
            if self.state not in (HostState.OPEN, HostState.RUNNING):
                raise ValueError("Não há partida aberta ou em andamento para cancelar.")
            self._finish("ABORTED", reason="host_shutdown")

    def shutdown(self) -> None:
        # /quit: igual o cancel(), mas sem erro se não tiver partida

        with self._lock:
            if self.state in (HostState.OPEN, HostState.RUNNING):
                self._finish("ABORTED", reason="host_shutdown")
            self._reset_to_idle()

    def describe(self) -> dict:
        # retorna os dados locais usados pelo comando /status

        with self._lock:
            info = {
                "state": self.state.value,
                "game_id": self.game_id,
                "max_players": self.max_players,
                "players": self._recipients(),
            }
            if self.hangman is not None:
                restante = None
                if self._turn_deadline is not None:
                    restante = max(0.0, self._turn_deadline - time.monotonic())
                info.update(
                    masked=self.hangman.masked,
                    lives=self.hangman.lives,
                    initial_lives=self.lives,
                    wrong_letters=list(self.hangman.wrong_letters),
                    turn=self.hangman.current_player,
                    seconds_left=restante,
                    scores=dict(self.hangman.scores),
                )
            return info

    # ------------------------------------------------------------------
    # handlers de mensagens recebidas (chamados pelo message_router, na thread da
    # conexão). `msg` é o dict já validado, use msg["src"] e msg["msg_id"].
    # o roteador garante antes que msg["src"] == conn.peer_id.
    # ------------------------------------------------------------------
    
    def handle_game_list(self, msg: dict) -> None:
        # GAME_LIST -> GAME_LIST_OK (repete o msg_id do pedido)
        with self._lock:
            if self.state == HostState.IDLE:
                games = []
            else:
                games = [{
                    "game_id": self.game_id,
                    "host": self.my_id,
                    "status": self.state.value,
                    "players": self._recipients(),
                    "max_players": self.max_players,
                    "lives": self.lives,                      # vidas INICIAIS
                    "word_length": len(self.hangman.word),    # nunca a palavra
                }]
            self._reply(msg, "GAME_LIST_OK", games=games)

    def handle_join(self, msg: dict) -> None:
        # processa a entrada de um jogador
        with self._lock:
            src = msg["src"]
            gid = msg.get("game_id")

            def recusar(motivo: str) -> None:
                self._reply(msg, "JOIN_ERR", game_id=gid, reason=motivo)

            if self.state == HostState.IDLE or gid != self.game_id:
                return recusar("unknown_game")
            if self.state == HostState.FINISHED:
                return recusar("game_finished")

            if self.state == HostState.OPEN:
                if src in self.lobby_players:
                    return recusar("already_joined")
                if len(self.lobby_players) >= self.max_players:
                    return recusar("game_full")
                self.lobby_players.append(src)
                self._reply(msg, "JOIN_OK", game_id=gid,
                            players=list(self.lobby_players), rejoin=False)
                logger.info("%s joined (%d/%d)", src, len(self.lobby_players),
                            self.max_players)
                return

            # RUNNING
            if src not in self.hangman.scores:
                return recusar("game_running")

            '''
            reconexão. O PeerServer n dispara on_disconnect quando uma conexao antiga
            é substituida, entao o jogador pode ainda constar como ativo, nesse caso
            só reenviamos o estado (add_player levantaria ValueError). o timer do turno
            n é mexid, entrar n muda de quem é a vez
            '''

            if src not in self.hangman.players:
                self.hangman.add_player(src)
            self._reply(msg, "JOIN_OK", game_id=gid,
                        players=list(self.hangman.players), rejoin=True)
            logger.info("%s rejoined (%d players)", src, len(self.hangman.players))
            self._send_state(None)

    def handle_leave(self, msg: dict) -> None:
        # processa a saída de um jogador
        with self._lock:
            src = msg["src"]
            gid = msg.get("game_id")

            if self.state == HostState.IDLE or gid != self.game_id:
                self._error(msg, "invalid_state", "LEAVE received but no matching game")
                return

            if self.state == HostState.OPEN:
                if src in self.lobby_players:
                    self.lobby_players.remove(src)
                    logger.info("%s left (%d/%d)", src, len(self.lobby_players),
                                self.max_players)
                self._reply(msg, "LEAVE_OK", game_id=gid)
                return

            if self.state == HostState.RUNNING and src in self.hangman.players:
                self._remover_jogador_ativo(src, "left the game", antes=lambda:
                                            self._reply(msg, "LEAVE_OK", game_id=gid))
                return

            self._reply(msg, "LEAVE_OK", game_id=gid)

    def handle_guess(self, msg: dict) -> None:
        # processa um guess e atualiza o estado da partida
        with self._lock:
            src = msg["src"]
            gid = msg.get("game_id")

            if self.state in (HostState.IDLE, HostState.OPEN) or gid != self.game_id:
                self._error(msg, "invalid_state", "GUESS received but no game running")
                return

            letter, word = msg.get("letter"), msg.get("word")
            palpite = letter if letter is not None else word
            if (letter is None) == (word is None) or not isinstance(palpite, str):
                self._error(msg, "bad_request",
                            "GUESS exige exatamente um campo texto: letter ou word")
                return

            if self.state == HostState.FINISHED:
                self._reply(msg, "GUESS_RESULT", game_id=gid, accepted=False,
                            reason="game_not_running")
                return

            if letter is not None:
                resultado = self.hangman.guess_letter(src, letter)
            else:
                resultado = self.hangman.guess_word(src, word)

            if not resultado.accepted:
                self._reply(msg, "GUESS_RESULT", game_id=gid, accepted=False,
                            reason=resultado.reason)
                return

            # invalida o timer do turno antes de enviar qualquer coisa
            self._cancel_turn_timer()
            self._reply(msg, "GUESS_RESULT", game_id=gid, accepted=True,
                        correct=resultado.correct)

            last_guess = {"player": src, "correct": resultado.correct}
            if letter is not None:
                last_guess["letter"] = letter
                rotulo = f"letter={letter}"
            else:
                last_guess["word"] = word
                rotulo = f"word={word}"
            logger.info("GUESS %s %s → %s, masked=%s", src, rotulo,
                        "correct" if resultado.correct else "wrong", self.hangman.masked)

            self._send_state(last_guess)

            status = self.hangman.status
            if status != "RUNNING":
                self._finish(status, winner=src if status == "WON" else None)
            else:
                self._start_turn_timer()

    def on_peer_lost(self, peer_id: str, reason: str = "STALE") -> None:
        # um peer ficou STALE ou desconectou (o p2p_client chama com conn.peer_id)

        with self._lock:
            if self.state == HostState.OPEN and peer_id in self.lobby_players:
                self.lobby_players.remove(peer_id)
                logger.info("Player %s %s — removed from lobby", peer_id, reason)
                return
            if self.state == HostState.RUNNING and peer_id in self.hangman.players:
                self._remover_jogador_ativo(peer_id, f"{reason} — removed from game")

    # ------------------------------------------------------------------
    # Auxiliares internos (assumem que o lock já ta seguro)
    # ------------------------------------------------------------------
    def _remover_jogador_ativo(self, peer_id: str, motivo: str, antes=None) -> None:
        # tira um jogador ativo de uma partida RUNNING (saída ou queda)

        era_da_vez = peer_id == self.hangman.current_player
        self.hangman.remove_player(peer_id)
        logger.info("Player %s %s", peer_id, motivo)
        if antes:
            antes()

        if not self.hangman.players:
            self._finish("ABORTED", reason="no_players")
            return
        self._send_state(None)
        if era_da_vez:
            self._start_turn_timer()

    def _make(self, type: str, dst: str, reply_to: str | None = None, **fields) -> dict:
        # monta o envelope: type, msg_id, ttl=1, src, dst + campos específicos

        msg_id = reply_to if reply_to is not None else str(uuid.uuid4())
        return {
            "type": type,
            "msg_id": msg_id,
            "ttl": 1,
            "src": self.my_id,
            "dst": dst,
            **fields,
        }

    def _recipients(self) -> list[str]:
        # cópia da lista certa de destinatários para o estado atual
        if self.state == HostState.OPEN:
            return list(self.lobby_players)
        if self.hangman is None:
            return []
        return list(self.hangman.players)

    def _send_one(self, type: str, dst: str, reply_to: str | None = None, **fields) -> bool:
        # envia uma mensagem, uma falha (False ou exceção) é registrada 
        try:
            ok = self.send(dst, self._make(type, dst, reply_to=reply_to, **fields))
        except Exception:
            logger.exception("Erro ao enviar %s a %s", type, dst)
            return False
        if not ok:
            logger.warning("Falha ao enviar %s a %s", type, dst)
        return bool(ok)

    def _reply(self, msg: dict, type: str, **fields) -> bool:
        # responde a `msg` repetindo o msg_id do pedido
        return self._send_one(type, msg["src"], reply_to=msg["msg_id"], **fields)

    def _error(self, msg: dict, code: str, text: str) -> bool:
        # ERROR tem msg_id próprio e ref_msg_id apontando para o pedido
        return self._send_one("ERROR", msg["src"], code=code, message=text,
                              ref_msg_id=msg.get("msg_id"))

    def _broadcast(self, type: str, to: list[str] | None = None, **fields) -> None:
        # envia a mesma mensagem a todos os destinatários
        for peer in list(self._recipients() if to is None else to):
            self._send_one(type, peer, **fields)

    def _build_state(self, last_guess: dict | None) -> dict:
        # monta os campos da mensagem STATE e incrementa o seq
        self.seq += 1
        h = self.hangman
        return {
            "game_id": self.game_id,
            "seq": self.seq,
            "masked": h.masked,
            "wrong_letters": list(h.wrong_letters),
            "lives": h.lives,
            "turn": h.current_player,
            "players": list(h.players),
            "last_guess": last_guess,
        }

    def _send_state(self, last_guess: dict | None) -> None:
        # monta e envia o estado atual aos jogadores
        self._broadcast("STATE", **self._build_state(last_guess))

    def _finish(self, result: str, winner: str | None = None,
                reason: str | None = None) -> None:
        # encerra a partida e agenda o retorno pra IDLE
        destinatarios = self._recipients()          # antes de mudar o estado
        self._cancel_turn_timer()
        self.state = HostState.FINISHED

        campos = dict(
            game_id=self.game_id,
            result=result,
            word=self.hangman.word,
            winner=winner,
            scores=dict(self.hangman.scores),
        )
        if result == "ABORTED":
            campos["reason"] = reason
        self._broadcast("GAME_OVER", to=destinatarios, **campos)

        if result == "WON":
            logger.info("Game %s finished: WON by %s", self.game_id, winner)
        elif result == "ABORTED":
            logger.info("Game %s finished: ABORTED (%s)", self.game_id, reason)
        else:
            logger.info("Game %s finished: %s", self.game_id, result)

        if reason == "no_players":
            self._reset_to_idle()                   # ninguém pra avisar, volta pra IDLE
        else:
            self._start_idle_timer()

    def _reset_to_idle(self) -> None:
        # volta pra IDLE e limpa o estado da partida
        self._cancel_turn_timer()
        self._idle_id += 1
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        self.state = HostState.IDLE
        self.game_id = None
        self.hangman = None
        self.lobby_players = []
        self.seq = 0
        self.max_players = 0

    # --- timers ---
    def _start_turn_timer(self) -> None:
       # inicia ou reinicia o timer do turno do jogador atual
        self._cancel_turn_timer()                   # invalida o token anterior
        token = self._turn_id
        self._turn_deadline = time.monotonic() + self.turn_timeout
        timer = threading.Timer(self.turn_timeout, self._turn_timeout, args=(token,))
        timer.daemon = True
        self._turn_timer = timer
        timer.start()
        logger.info("Turn: %s (timeout %s s)", self.hangman.current_player,
                    self.turn_timeout)

    def _cancel_turn_timer(self) -> None:
        # cancela o timer e invalida seu callback
        self._turn_id += 1
        if self._turn_timer is not None:
            self._turn_timer.cancel()
            self._turn_timer = None
        self._turn_deadline = None

    def _turn_timeout(self, token: int) -> None:
        # callback do timer
        with self._lock:
            if token != self._turn_id or self.state != HostState.RUNNING:
                return
            jogador = self.hangman.current_player
            logger.info("Turn timeout for %s — skipping", jogador)
            self.hangman.skip_turn()
            self._send_state({"player": jogador, "timeout": True})
            self._start_turn_timer()

    def _start_idle_timer(self) -> None:
        # agenda a volta a IDLE em finished_ttl segundos
        self._idle_id += 1
        token = self._idle_id
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        timer = threading.Timer(self.finished_ttl, self._idle_after_finished, args=(token,))
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _idle_after_finished(self, token: int) -> None:
        with self._lock:
            if token != self._idle_id or self.state != HostState.FINISHED:
                return
            self._reset_to_idle()