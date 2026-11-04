"""Client du jeu du pendu en réseau (reprise de session, TLS, chronomètre, salles).

Le client terminal joue en solo ; les salles (joueur contre joueur) se jouent
dans l'interface graphique (gui.py), qui utilise la classe Game de ce fichier.

Lancement : python3 client.py [hôte] [port] [--tls] [--ca server.crt]
  ex. python3 client.py              -> 127.0.0.1:8080 (port du serveur Go)
      python3 client.py ::1 8080     -> test en IPv6
      python3 client.py --tls        -> connexion chiffrée, serveur vérifié avec server.crt

En jeu : tape une lettre, ou
  quit        abandonner la partie
  /coupe      (test) couper la connexion brutalement
  /perte X    (test) jouer X, perdre la réponse et renvoyer la même requête

La session (session_id, resume_token, compteur de requêtes) est sauvegardée dans
sessions/<pseudo>.json : relance le client avec le même pseudo pour reprendre.
"""
import argparse
import json
import math
import queue
import re
import socket
import ssl
import threading
import time
import unicodedata
from pathlib import Path

DEFAULT_PORT = 8080  # port du serveur Go (config.yaml de ton binôme)
RECONNECT_TRIES = 5
RECONNECT_DELAY = 2  # secondes entre deux tentatives
REPLY_TIMEOUT = 15   # secondes d'attente maximum pour une réponse
SESSIONS_DIR = Path(__file__).with_name("sessions")
DEFAULT_CA = Path(__file__).with_name("server.crt")


def make_tls_context(cafile=DEFAULT_CA):
    """Contexte TLS qui ne fait confiance qu'au certificat de notre serveur (auto-signé)."""
    ctx = ssl.create_default_context(cafile=str(cafile))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def base_letter(ch):
    """'É' -> 'E' : le serveur compare les lettres sans accent."""
    return unicodedata.normalize("NFD", ch)[0].upper()


def hangman_stage(attempts_left, max_attempts):
    """Étape du dessin (0 à 6), quel que soit le nombre d'essais du serveur (6, 10...)."""
    if attempts_left <= 0:
        return 6
    errors = max(0, max_attempts - attempts_left)
    return min(5, math.ceil(errors * 6 / max_attempts))


PENDU = [
    "\n\n\n\n\n=========",
    "\n      |\n      |\n      |\n      |\n=========",
    "  +---+\n      |\n      |\n      |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n      |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n  |   |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n=========",
    "  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n=========",
]


class SessionLost(Exception):
    """Le serveur refuse la reprise (session expirée ou jeton invalide)."""


class ServerTimeout(ConnectionError):
    """Le serveur a fermé la connexion pour inactivité (ERROR TIMEOUT non sollicité)."""


class Connection:
    """Socket TCP + découpage du flux en messages (un JSON par ligne).

    Un thread lecteur lit la socket en continu, car le serveur peut envoyer un
    message que l'on n'a pas demandé (coup de l'adversaire, déconnexion...) :
      - un message avec request_id est la réponse à une de nos requêtes : il va
        dans une file où request() l'attend ;
      - un message sans request_id est poussé par le serveur : il part vers
        on_event (l'interface).
    """

    def __init__(self, host, port, tls=None, on_event=None, on_close=None):
        sock = socket.create_connection((host, port), timeout=10)
        if tls:
            # Poignée de main TLS : vérifie le certificat et que son nom correspond à host
            try:
                sock = tls.wrap_socket(sock, server_hostname=host)
            except OSError:
                sock.close()
                raise
        sock.settimeout(None)  # le lecteur attend sans limite ; request() a son propre délai
        self.sock = sock
        self.on_event = on_event or (lambda msg: None)
        self.on_close = on_close or (lambda conn: None)
        self.replies = queue.Queue()
        self.closed = threading.Event()
        self.close_reason = None
        self.wlock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def send(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self.wlock:
            self.sock.sendall(data)

    def _read_loop(self):
        # TCP est un flux : on accumule jusqu'à avoir une ligne complète.
        buf = b""
        try:
            while True:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("le serveur a fermé la connexion")
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._dispatch(line)
        except OSError as e:
            if self.close_reason is None:
                self.close_reason = e
        finally:
            self.closed.set()
            self.replies.put(None)  # réveille une requête en attente
            self.on_close(self)

    def _dispatch(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return  # ligne illisible : ignorée
        if not isinstance(msg, dict):
            return
        if msg.get("request_id") is None:
            if msg.get("type") == "ERROR" and msg.get("code") == "TIMEOUT":
                # le serveur va fermer la connexion pour inactivité
                self.close_reason = ServerTimeout("le serveur a fermé la connexion après une longue inactivité")
                return
            if msg.get("type") != "ERROR":
                self.on_event(msg)  # message poussé (ROOM...)
                return
        self.replies.put(msg)

    def receive(self, rid, timeout=REPLY_TIMEOUT):
        """Attend la réponse à la requête rid (les réponses périmées sont ignorées)."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("pas de réponse du serveur")
            try:
                msg = self.replies.get(timeout=left)
            except queue.Empty:
                raise TimeoutError("pas de réponse du serveur") from None
            if msg is None:
                self.replies.put(None)
                raise self.close_reason or ConnectionError("le serveur a fermé la connexion")
            got = msg.get("request_id")
            if rid is None or got is None or got == rid:
                return msg

    def close(self):
        """Ferme la connexion ; shutdown réveille le thread lecteur bloqué dans recv()."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Game:
    """Le joueur côté client : connexion, sauvegarde locale et reprise automatique.

    Il joue soit une partie solo (session_id), soit en salle contre un autre
    joueur (room_id). Dans les deux cas, un jeton secret permet de reprendre
    la partie après une coupure.
    """

    def __init__(self, host, port, player, log=print, tls=None, on_event=None, on_disconnect=None):
        self.host, self.port, self.player = host, port, player
        self.log = log  # où afficher les messages de connexion (terminal ou interface graphique)
        self.tls = tls  # contexte TLS, ou None pour une connexion en clair
        self.on_event = on_event or (lambda msg: None)          # message poussé par le serveur
        self.on_disconnect = on_disconnect or (lambda conn: None)  # connexion perdue
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", player)[:32] or "joueur"
        self.file = SESSIONS_DIR / f"{safe}.json"
        self.conn = None
        self.sid = None       # partie solo
        self.token = None
        self.room_id = None   # partie en salle
        self.room_token = None
        self.next_id = 1
        self.max_attempts = 0  # appris du serveur à NEW_GAME (6 pour le faux serveur, 10 pour le Go)
        self.last_resume = None

    # --- sauvegarde locale de la partie ---
    def save(self):
        SESSIONS_DIR.mkdir(exist_ok=True)
        self.file.write_text(json.dumps({
            "session_id": self.sid,
            "resume_token": self.token,
            "room_id": self.room_id,
            "room_token": self.room_token,
            "next_id": self.next_id,
            "max_attempts": self.max_attempts,
        }))

    def load(self):
        try:
            data = json.loads(self.file.read_text())
            self.sid, self.token = data.get("session_id"), data.get("resume_token")
            self.room_id, self.room_token = data.get("room_id"), data.get("room_token")
            self.next_id = int(data.get("next_id", 1))
            self.max_attempts = int(data.get("max_attempts", 0))
            return bool(self.sid or self.room_id)
        except (OSError, ValueError, TypeError):
            return False

    def forget(self):
        self.file.unlink(missing_ok=True)
        self.sid = self.token = None
        self.room_id = self.room_token = None
        self.max_attempts = 0

    def track(self, resp):
        """Retient le nombre d'essais maximum vu dans les réponses STATE."""
        left = resp.get("attempts_left") if isinstance(resp, dict) else None
        if isinstance(left, int) and left > self.max_attempts:
            self.max_attempts = left
        return resp

    def stage(self, state):
        return hangman_stage(state["attempts_left"], max(self.max_attempts, state["attempts_left"], 1))

    # --- connexion ---
    def new_id(self):
        rid = self.next_id
        self.next_id += 1
        if self.sid or self.room_id:
            self.save()  # le compteur survit à un redémarrage du client
        return rid

    def drop(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    @property
    def connected(self):
        return self.conn is not None and not self.conn.closed.is_set()

    @property
    def tls_version(self):
        if self.tls and self.connected:
            try:
                return self.conn.sock.version()
            except (OSError, AttributeError):
                return None
        return None

    def attach(self):
        """Ouvre une connexion ; si une partie existe, la reprend (RESUME) et renvoie son état."""
        self.conn = Connection(self.host, self.port, self.tls, self.on_event, self.on_disconnect)
        if self.room_id:
            msg = {"action": "RESUME", "room_id": self.room_id, "resume_token": self.room_token}
        elif self.sid:
            msg = {"action": "RESUME", "session_id": self.sid, "resume_token": self.token}
        else:
            return None
        msg["request_id"] = self.new_id()
        self.conn.send(msg)
        resp = self.conn.receive(msg["request_id"])
        if resp.get("type") == "ERROR":
            raise SessionLost(resp)
        return self.track(resp)

    def _with_retry(self, action):
        """Exécute action() ; en cas de coupure : reconnexion, RESUME, puis on recommence."""
        attempt = 0
        while True:
            try:
                if not self.connected:
                    self.drop()
                    self.last_resume = self.attach()
                    if self.last_resume is not None:
                        self.log("[i] Reconnecté, partie reprise.")
                return action()
            except OSError as e:  # coupure, timeout, serveur injoignable...
                self.drop()
                if isinstance(e, ssl.SSLError) and not isinstance(e, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
                    # Certificat refusé ou protocole incompatible : réessayer ne changera rien
                    raise ConnectionError(f"erreur TLS : {e.reason or e}. Vérifie le certificat "
                                          "et que le serveur est lancé avec --tls") from e
                attempt += 1
                if attempt > RECONNECT_TRIES:
                    raise ConnectionError(f"serveur injoignable ({e})")
                if isinstance(e, ServerTimeout) and attempt == 1:
                    self.log("[i] Connexion fermée par le serveur pour inactivité. Reconnexion...")
                    continue  # le serveur est là : on se reconnecte tout de suite
                self.log(f"[!] Connexion perdue ({e}). Tentative {attempt}/{RECONNECT_TRIES} dans {RECONNECT_DELAY} s...")
                time.sleep(RECONNECT_DELAY)

    def request(self, msg, rid=None):
        """Envoie une requête et renvoie la réponse.

        En cas de coupure : reconnexion, RESUME, puis renvoi de la MÊME requête
        (même request_id). Si le serveur l'avait déjà traitée, il renvoie l'ancienne
        réponse sans rejouer le coup.
        """
        msg = dict(msg, request_id=rid if rid is not None else self.new_id())

        def act():
            self.conn.send(msg)
            resp = self.conn.receive(msg["request_id"])
            if resp.get("type") == "BYE":
                self.drop()  # le serveur ferme la connexion après BYE
            return self.track(resp)

        return self._with_retry(act)

    def reconnect(self):
        """Se reconnecte et reprend la partie sans rien jouer. Renvoie l'état repris (ou None)."""
        self.drop()
        self.last_resume = None
        self._with_retry(lambda: None)
        return self.last_resume

    # --- partie solo ---
    def new_game(self):
        self.max_attempts = 0
        self.room_id = self.room_token = None
        resp = self.request({"action": "NEW_GAME", "player": self.player})
        if resp.get("type") == "STATE":
            self.sid, self.token = resp["session_id"], resp["resume_token"]
            self.max_attempts = resp["attempts_left"]
            self.save()
        return resp

    # --- salles (joueur contre joueur) ---
    def list_rooms(self):
        return self.request({"action": "LIST_ROOMS"})

    def _enter_room(self, resp):
        if resp.get("type") == "ROOM":
            self.room_id, self.room_token = resp["room_id"], resp["resume_token"]
            self.sid = self.token = None
            self.save()
        return resp

    def create_room(self, name=""):
        return self._enter_room(self.request({"action": "CREATE_ROOM", "player": self.player, "room_name": name}))

    def join_room(self, room_id):
        return self._enter_room(self.request({"action": "JOIN_ROOM", "player": self.player, "room_id": room_id}))

    def room_letter(self, letter):
        return self.request({"action": "ROOM_LETTER", "room_id": self.room_id, "letter": letter})

    def room_status(self):
        return self.request({"action": "STATUS", "room_id": self.room_id})

    def leave_room(self):
        resp = self.request({"action": "LEAVE_ROOM", "room_id": self.room_id})
        self.forget()
        return resp

    # --- commandes de test pour la démo ---
    def cut(self):
        """Coupe la connexion sans prévenir le serveur."""
        if self.conn:
            self.conn.close()

    def lost_response(self, msg):
        """Partie 6.1 : la requête arrive au serveur mais la réponse est perdue."""
        rid = self.new_id()
        if not self.connected:
            self.drop()
            self.attach()
        self.conn.send(dict(msg, request_id=rid))
        time.sleep(0.3)  # le serveur a le temps de traiter le coup
        self.conn.close()
        self.log(f"[test] Requête {rid} envoyée, réponse perdue. On la renvoie avec le même request_id...")
        return self.request(msg, rid=rid)


def show(state, game):
    print()
    print(PENDU[game.stage(state)])
    print(f"\nMot : {state['masked_word']}")
    print(f"Lettres tentées : {', '.join(state['tried_letters']) or '-'}")
    print(f"Essais restants : {state['attempts_left']}")
    ms = state.get("time_left_ms")
    if ms is not None and state["status"] == "IN_PROGRESS":
        secs = math.ceil(ms / 1000)
        print(f"Temps restant : {secs // 60}:{secs % 60:02d} (décompté par le serveur)")
    print(f">> {state['message']}")
    if state.get("replayed"):
        print("   (réponse renvoyée par le serveur : le coup n'a pas été compté deux fois)")


def print_error(resp):
    print(f"[{resp['code']}] {resp['message']}")


def main():
    parser = argparse.ArgumentParser(description="Client du pendu en réseau")
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument("--tls", action="store_true", help="connexion chiffrée")
    parser.add_argument("--ca", default=str(DEFAULT_CA), help="certificat du serveur à accepter")
    args = parser.parse_args()

    tls = None
    if args.tls:
        try:
            tls = make_tls_context(args.ca)
        except (OSError, ssl.SSLError) as e:
            print(f"Certificat {args.ca} introuvable ou invalide ({e}).")
            return

    try:
        player = input("Ton pseudo : ").strip() or "Joueur"
    except (KeyboardInterrupt, EOFError):
        return
    game = Game(args.host, args.port, player, tls=tls)

    try:
        state = None
        if game.load() and game.room_id:
            print(f"Tu as une partie en salle en cours ({game.room_id}) : reprends-la avec l'interface graphique (python3 gui.py).")
            return
        if game.sid:
            answer = input("Tu as une partie en cours. La reprendre ? [O/n] ").strip().lower()
            if answer in ("", "o", "oui", "y"):
                try:
                    state = game.attach()
                    print("[i] Session reprise.")
                except SessionLost as e:
                    print_error(e.args[0])
                    print("On lance une nouvelle partie.")
                    game.forget()
            else:
                game.forget()

        if state is None:
            state = game.new_game()
            if state.get("type") == "ERROR":
                print_error(state)
                return

        if game.tls_version:
            print(f"[i] Connexion chiffrée ({game.tls_version}), certificat du serveur vérifié.")
        elif not tls:
            print("[i] Connexion en clair (lance avec --tls pour la chiffrer).")
        show(state, game)
        while state["status"] == "IN_PROGRESS":
            cmd = input("\nLettre (ou quit) : ").strip()
            if cmd.lower() == "quit":
                bye = game.request({"action": "QUIT", "session_id": game.sid})
                # BYE, ou STATE LOST si le temps était déjà écoulé
                print(f"{bye.get('message')} Le mot était : {bye.get('secret_word')}")
                game.forget()
                return
            if cmd == "/coupe":
                game.cut()
                print("[test] Connexion coupée. Joue une lettre pour voir la reconnexion.")
                continue
            if cmd.startswith("/perte"):
                resp = game.lost_response({"action": "LETTER", "session_id": game.sid,
                                           "letter": cmd[len("/perte"):].strip()})
            else:
                resp = game.request({"action": "LETTER", "session_id": game.sid, "letter": cmd})
            if resp.get("type") == "ERROR":
                print_error(resp)
                continue
            state = resp
            show(state, game)

        if state["status"] == "LOST" and state.get("secret_word"):
            print(f"Le mot était : {state['secret_word']}")
        game.forget()

    except SessionLost as e:
        print_error(e.args[0])
        print("Impossible de reprendre la partie.")
        game.forget()
    except OSError as e:  # inclut ConnectionError et les erreurs TLS
        print(f"\nConnexion perdue : {e}")
        if game.sid:
            print("Ta partie est sauvegardée : relance le client avec le même pseudo quand le serveur est revenu.")
    except (KeyboardInterrupt, EOFError):
        print()
        if game.sid:
            print("Partie sauvegardée : relance le client avec le même pseudo pour la reprendre.")
    finally:
        game.drop()


if __name__ == "__main__":
    main()
