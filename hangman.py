import hashlib
import unicodedata

from dataclasses import dataclass


@dataclass
class GuessResult:
    accepted: bool
    reason: str | None = None     # só quando accepted == False
    correct: bool | None = None   # só quando accepted == True


class Hangman:
    def __init__(self, word: str, lives: int = 6):
        if lives < 1:
            raise ValueError("A partida precisa de pelo menos 1 vida.")

        self.word = self.normalize_word(word)
        self.lives = lives
        self.right_letters: set[str] = set()
        self.wrong_letters: list[str] = []
        self.players: list[str] = []
        self.scores: dict[str, int] = {}
        self.current_player: str | None = None

    # normaliza e valida a palavra, removendo acentos e convertendo para minúsculas
    @staticmethod
    def normalize_word(raw: str) -> str:
        normalizado = unicodedata.normalize("NFD", raw).lower()
        sem_acento = ''.join([c for c in normalizado if not unicodedata.combining(c)])
        sem_acento = sem_acento.strip()
        if len(sem_acento) < 3 or len(sem_acento) > 20:
            raise ValueError("A palavra deve ter entre 3 e 20 letras.")
        if not all(c.isalpha() and c.isascii() for c in sem_acento):
            raise ValueError("A palavra deve conter apenas letras de a-z.")
        return sem_acento

    # consultas
    @property
    def masked(self) -> str:
        return ''.join(c if c in self.right_letters else '_' for c in self.word)

    @property
    def word_hash(self) -> str:
        return hashlib.sha256(self.word.encode()).hexdigest()

    @property
    def status(self) -> str:
        if self.lives <= 0:
            return "LOST"
        if all(c in self.right_letters for c in self.word):
            return "WON"
        return "RUNNING"

    # ações
    def start(self, players: list[str]) -> None:
        # inicia a partida com os jogadores na ordem de entrada.
        if not players:
            raise ValueError("É necessário pelo menos um jogador para iniciar a partida.")
        if len(set(players)) != len(players):
            raise ValueError("Os identificadores dos jogadores devem ser únicos.")

        self.players = players.copy()
        self.scores = {player: 0 for player in players}
        self.current_player = players[0]

    def guess_letter(self, player: str, letter: str) -> GuessResult:
        if self.status != "RUNNING":
            return GuessResult(accepted=False, reason="game_not_running")

        if player != self.current_player:
            return GuessResult(accepted=False, reason="not_your_turn")

        if len(letter) != 1 or not ("a" <= letter <= "z"):
            return GuessResult(accepted=False, reason="invalid_letter")

        if letter in self.right_letters or letter in self.wrong_letters:
            return GuessResult(accepted=False, reason="letter_already_used")

        # parte 2 - execução
        ocorrencias = self.word.count(letter)
        if ocorrencias > 0:
            self.right_letters.add(letter)
            self.scores[player] += ocorrencias
        else:
            self.wrong_letters.append(letter)
            self.lives -= 1

        self._next_turn()
        return GuessResult(accepted=True, correct=ocorrencias > 0)

    def _next_turn(self) -> None:
        # passa a vez pro proximo jogador
        i = self.players.index(self.current_player)
        self.current_player = self.players[(i + 1) % len(self.players)]

    def guess_word(self, player: str, word: str) -> GuessResult:
        if self.status != "RUNNING":
            return GuessResult(accepted=False, reason="game_not_running")

        if player != self.current_player:
            return GuessResult(accepted=False, reason="not_your_turn")

        if not word or not all("a" <= c <= "z" for c in word):
            return GuessResult(accepted=False, reason="invalid_word")

        # parte 2: aplicação
        acertou = word == self.word

        if acertou:
            ocultas = sum(1 for c in self.word if c not in self.right_letters)
            self.scores[player] += ocultas
            self.right_letters.update(self.word)
        else:
            self.lives -= 1

        self._next_turn()
        return GuessResult(accepted=True, correct=acertou)

    def skip_turn(self) -> None:
        # timeout, passa a vez
        if self.status != "RUNNING" or self.current_player is None:
            return
        self._next_turn()

    def remove_player(self, player: str) -> None:
        # tira o jogador da ordem de jogada, mas mantem a pontuação dele caso ele volte a entrar
        if player not in self.players:
            return
        i = self.players.index(player)
        era_da_vez = player == self.current_player
        self.players.pop(i)

        if not self.players:
            self.current_player = None
        elif era_da_vez:
            # apos o pop, quem estava depois dele ocupa a posição i
            self.current_player = self.players[i % len(self.players)]

    def add_player(self, player: str) -> None:
        # rejoin, entra no fim da fila, mantendo a pontuação se existir
        if player in self.players:
            raise ValueError("O jogador já está na partida.")
        self.players.append(player)
        self.scores.setdefault(player, 0)
        if self.current_player is None:
            self.current_player = player
