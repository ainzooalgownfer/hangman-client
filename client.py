"""Client du jeu du pendu en réseau (reprise de session + TLS).

Lancement : python3 client.py [hôte] [port] [--tls] [--ca server.crt]
  ex. python3 client.py              -> 127.0.0.1:5050
      python3 client.py ::1 5050     -> test en IPv6
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
import re
import socket
import ssl
import time
from pathlib import Path

RECONNECT_TRIES = 5
RECONNECT_DELAY = 2  # secondes entre deux tentatives
SESSIONS_DIR = Path(__file__).with_name("sessions")
DEFAULT_CA = Path(__file__).with_name("server.crt")


def make_tls_context(cafile=DEFAULT_CA):
    """Contexte TLS qui ne fait confiance qu'au certificat de notre serveur (auto-signé)."""
    ctx = ssl.create_default_context(cafile=str(cafile))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx

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


class Connection:
    """Gère la socket TCP et le découpage du flux en messages (un JSON par ligne)."""

    def __init__(self, host, port, tls=None):
        sock = socket.create_connection((host, port), timeout=10)
        if tls:
            # Poignée de main TLS : vérifie le certificat et que son nom correspond à host
            try:
                sock = tls.wrap_socket(sock, server_hostname=host)
            except OSError:
                sock.close()
                raise
        self.sock = sock
        self.buf = b""

    def send(self, msg):
        self.sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))

    def receive(self):
        # TCP est un flux : on accumule jusqu'à avoir une ligne complète.
        # S'il y a plusieurs messages dans le buffer, les suivants restent pour le prochain appel.
        while True:
            while b"\n" not in self.buf:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("le serveur a fermé la connexion")
                self.buf += data
            line, self.buf = self.buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                return json.loads(line)
            except ValueError:
                print(f"[!] Réponse illisible ignorée : {line[:80]!r}")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Game:
    """Une partie côté client : connexion, sauvegarde locale et reprise automatique."""

    def __init__(self, host, port, player, log=print, tls=None):
        self.host, self.port, self.player = host, port, player
        self.log = log  # où afficher les messages de connexion (terminal ou interface graphique)
        self.tls = tls  # contexte TLS, ou None pour une connexion en clair
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", player)[:32] or "joueur"
        self.file = SESSIONS_DIR / f"{safe}.json"
        self.conn = None
        self.sid = None
        self.token = None
        self.next_id = 1

    # --- sauvegarde locale de la session ---
    def save(self):
        SESSIONS_DIR.mkdir(exist_ok=True)
        self.file.write_text(json.dumps({
            "session_id": self.sid,
            "resume_token": self.token,
            "next_id": self.next_id,
        }))

    def load(self):
        try:
            data = json.loads(self.file.read_text())
            self.sid, self.token = data["session_id"], data["resume_token"]
            self.next_id = int(data.get("next_id", 1))
            return True
        except (OSError, ValueError, KeyError):
            return False

    def forget(self):
        self.file.unlink(missing_ok=True)
        self.sid = self.token = None

    # --- connexion ---
    def new_id(self):
        rid = self.next_id
        self.next_id += 1
        if self.sid:
            self.save()  # le compteur survit à un redémarrage du client
        return rid

    def drop(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    @property
    def tls_version(self):
        if self.tls and self.conn:
            try:
                return self.conn.sock.version()
            except (OSError, AttributeError):
                return None
        return None

    def attach(self):
        """Ouvre une connexion ; si une partie existe, la reprend (RESUME) et renvoie son état."""
        self.conn = Connection(self.host, self.port, self.tls)
        if not self.sid:
            return None
        self.conn.send({"action": "RESUME", "request_id": self.new_id(),
                        "session_id": self.sid, "resume_token": self.token})
        resp = self.conn.receive()
        if resp.get("type") == "ERROR":
            raise SessionLost(resp)
        return resp

    def request(self, msg, rid=None):
        """Envoie une requête et renvoie la réponse.

        En cas de coupure : reconnexion, RESUME, puis renvoi de la MÊME requête
        (même request_id). Si le serveur l'avait déjà traitée, il renvoie l'ancienne
        réponse sans rejouer le coup.
        """
        msg = dict(msg, request_id=rid if rid is not None else self.new_id())
        attempt = 0
        while True:
            try:
                if self.conn is None:
                    if self.attach() is not None:
                        self.log("[i] Reconnecté, session reprise.")
                self.conn.send(msg)
                return self.conn.receive()
            except OSError as e:  # coupure, timeout, serveur injoignable...
                self.drop()
                if isinstance(e, ssl.SSLError) and not isinstance(e, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
                    # Certificat refusé ou protocole incompatible : réessayer ne changera rien
                    raise ConnectionError(f"erreur TLS : {e.reason or e}. Vérifie le certificat "
                                          "et que le serveur est lancé avec --tls") from e
                attempt += 1
                if attempt > RECONNECT_TRIES:
                    raise ConnectionError(f"serveur injoignable ({e})")
                self.log(f"[!] Connexion perdue ({e}). Tentative {attempt}/{RECONNECT_TRIES} dans {RECONNECT_DELAY} s...")
                time.sleep(RECONNECT_DELAY)

    def new_game(self):
        resp = self.request({"action": "NEW_GAME", "player": self.player})
        if resp.get("type") == "STATE":
            self.sid, self.token = resp["session_id"], resp.get("resume_token")
            self.save()
        return resp

    # --- commandes de test pour la démo ---
    def cut(self):
        """Ferme la socket sans prévenir : la prochaine requête échoue et déclenche la reconnexion."""
        if self.conn:
            self.conn.sock.close()

    def lost_response(self, msg):
        """Partie 6.1 : la requête arrive au serveur mais la réponse est perdue."""
        rid = self.new_id()
        if self.conn is None:
            self.attach()
        self.conn.send(dict(msg, request_id=rid))
        time.sleep(0.3)  # le serveur a le temps de traiter le coup
        self.conn.sock.close()
        self.log(f"[test] Requête {rid} envoyée, réponse perdue. On la renvoie avec le même request_id...")
        return self.request(msg, rid=rid)


def show(state):
    erreurs = 6 - state["attempts_left"]
    print()
    print(PENDU[max(0, min(erreurs, 6))])
    print(f"\nMot : {state['masked_word']}")
    print(f"Lettres tentées : {', '.join(state['tried_letters']) or '-'}")
    print(f"Essais restants : {state['attempts_left']}")
    print(f">> {state['message']}")
    if state.get("replayed"):
        print("   (réponse renvoyée par le serveur : le coup n'a pas été compté deux fois)")


def print_error(resp):
    print(f"[{resp['code']}] {resp['message']}")


def main():
    parser = argparse.ArgumentParser(description="Client du pendu en réseau")
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=5050)
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
        if game.load():
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
        show(state)
        while state["status"] == "IN_PROGRESS":
            cmd = input("\nLettre (ou quit) : ").strip()
            if cmd.lower() == "quit":
                bye = game.request({"action": "QUIT", "session_id": game.sid})
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
            show(state)

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
