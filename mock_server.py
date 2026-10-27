"""Faux serveur du pendu, pour tester le client sans attendre le serveur Go.

Même contrat que la spec : un objet JSON par ligne, terminé par \n.
Lancement : python3 mock_server.py [--port 5050] [--ttl 300] [--tls]
  --tls : chiffre les connexions avec server.crt / server.key (voir gen_cert.sh)

Étape 2 (reprise de session) :
- une session est rattachée à UNE connexion (NEW_GAME ou RESUME) ;
- à la déconnexion, elle est détachée et reste valable --ttl secondes ;
- RESUME exige le resume_token secret reçu au NEW_GAME (sinon PLAYER_NOT_ALLOWED) ;
- une requête dont le request_id a déjà été traité n'est pas rejouée :
  le serveur renvoie la réponse déjà calculée (cas de la réponse perdue).
"""
import argparse
import itertools
import json
import random
import secrets
import socket
import ssl
import threading
import time
import uuid

WORDS = ["TEXTE", "RESEAU", "SOCKET", "PENDU", "SERVEUR", "PAQUET"]
TTL = 300  # durée de validité d'une session détachée, en secondes

sessions = {}
lock = threading.Lock()
conn_ids = itertools.count(1)


def masked(s):
    return " ".join(c if c in s["tried"] else "_" for c in s["word"])


def state(s, message, rid):
    r = {
        "type": "STATE",
        "request_id": rid,
        "session_id": s["id"],
        "masked_word": masked(s),
        "attempts_left": s["left"],
        "tried_letters": sorted(s["tried"]),
        "status": s["status"],
        "message": message,
    }
    if s["status"] in ("WON", "LOST"):
        r["secret_word"] = s["word"]
    return r


def error(code, message, rid=None):
    return {"type": "ERROR", "request_id": rid, "code": code, "message": message}


def new_game(req, rid, cid):
    s = {
        "id": str(uuid.uuid4()),
        "token": secrets.token_hex(16),
        "player": str(req.get("player", "?")),
        "word": random.choice(WORDS),
        "tried": set(),
        "left": 6,
        "status": "IN_PROGRESS",
        "owner": cid,          # connexion à laquelle la session est rattachée
        "last_seen": time.time(),
        "done": {},            # request_id -> réponse déjà envoyée
    }
    with lock:
        sessions[s["id"]] = s
    r = state(s, "Partie lancée !", rid)
    r["resume_token"] = s["token"]
    return r


def play(s, action, req, rid):
    """Applique LETTER ou QUIT à une session (appelée sous verrou)."""
    if s["status"] != "IN_PROGRESS":
        return error("INVALID_STATE", "La partie est déjà terminée.", rid)

    if action == "QUIT":
        s["status"] = "ABANDONED"
        return {"type": "BYE", "request_id": rid, "session_id": s["id"],
                "status": "ABANDONED", "secret_word": s["word"],
                "message": "Partie abandonnée."}

    letter = str(req.get("letter", "")).upper()
    if len(letter) != 1 or not ("A" <= letter <= "Z"):
        return error("INVALID_LETTER", "Une seule lettre (A-Z) est attendue.", rid)
    if letter in s["tried"]:
        return error("ALREADY_GUESSED", "Lettre déjà testée.", rid)

    s["tried"].add(letter)
    if letter in s["word"]:
        if all(c in s["tried"] for c in s["word"]):
            s["status"], msg = "WON", "Gagné !"
        else:
            msg = "Bien vu !"
    else:
        s["left"] -= 1
        if s["left"] == 0:
            s["status"], msg = "LOST", "Perdu..."
        else:
            msg = "Raté !"
    return state(s, msg, rid)


def handle(req, cid):
    rid = req.get("request_id")
    if rid is not None and (not isinstance(rid, int) or isinstance(rid, bool)):
        return error("INVALID_MESSAGE", "request_id doit être un entier.")
    action = req.get("action")

    if action == "NEW_GAME":
        return new_game(req, rid, cid)

    if action not in ("LETTER", "RESUME", "QUIT"):
        return error("UNKNOWN_ACTION", f"Action inconnue : {action!r}.", rid)

    with lock:
        s = sessions.get(req.get("session_id"))
        if s is None:
            return error("UNKNOWN_SESSION", "Session inexistante ou expirée.", rid)
        s["last_seen"] = time.time()

        if action == "RESUME":
            if not secrets.compare_digest(str(req.get("resume_token", "")), s["token"]):
                return error("PLAYER_NOT_ALLOWED", "Jeton de reprise invalide : cette session ne t'appartient pas.", rid)
            s["owner"] = cid  # la session passe sur la nouvelle connexion
            return state(s, "Partie reprise.", rid)

        if s["owner"] != cid:
            return error("PLAYER_NOT_ALLOWED", "Session non rattachée à cette connexion : envoie RESUME d'abord.", rid)

        if rid is not None and rid in s["done"]:
            print(f"    (requête {rid} déjà traitée : réponse renvoyée sans rejouer le coup)")
            return dict(s["done"][rid], replayed=True)

        resp = play(s, action, req, rid)
        if rid is not None:
            s["done"][rid] = resp
        return resp


def serve(conn, addr, tls=None):
    cid = next(conn_ids)
    if tls:
        # Poignée de main TLS dans le thread du client, pour ne pas bloquer accept()
        try:
            conn.settimeout(10)
            conn = tls.wrap_socket(conn, server_side=True)
            conn.settimeout(None)
        except (ssl.SSLError, OSError) as e:
            print(f"[!] Connexion {cid} refusée : poignée de main TLS échouée ({addr[0]}) : {e}")
            conn.close()
            return
        print(f"[+] Connexion {cid} ouverte ({addr[0]}), chiffrée en {conn.version()}")
    else:
        print(f"[+] Connexion {cid} ouverte ({addr[0]})")
    buf = b""
    try:
        with conn:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                # Framing : on ne traite que les lignes complètes, le reste attend dans buf
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        req = json.loads(line)
                        resp = handle(req, cid) if isinstance(req, dict) else error("INVALID_MESSAGE", "Objet JSON attendu.")
                    except ValueError:
                        resp = error("INVALID_MESSAGE", "JSON invalide.")
                    print(f"    [{cid}] -> {line.decode(errors='replace')}")
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass  # coupure brutale côté client : on traite comme une déconnexion
    finally:
        with lock:
            for s in sessions.values():
                if s["owner"] == cid:
                    s["owner"] = None
                    s["last_seen"] = time.time()
                    print(f"    session {s['id'][:8]} détachée, valable {TTL} s")
        print(f"[-] Connexion {cid} fermée")


def reaper():
    """Supprime les sessions détachées depuis plus de TTL secondes."""
    while True:
        time.sleep(1)
        now = time.time()
        with lock:
            expired = [k for k, s in sessions.items()
                       if s["owner"] is None and now - s["last_seen"] > TTL]
            for k in expired:
                del sessions[k]
                print(f"[x] Session {k[:8]} expirée")


def main():
    global TTL
    parser = argparse.ArgumentParser(description="Faux serveur du pendu")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--ttl", type=int, default=300, help="validité d'une session détachée (s)")
    parser.add_argument("--tls", action="store_true", help="chiffrer les connexions avec TLS")
    parser.add_argument("--cert", default="server.crt")
    parser.add_argument("--key", default="server.key")
    args = parser.parse_args()
    TTL = args.ttl

    tls = None
    if args.tls:
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            tls.load_cert_chain(args.cert, args.key)
        except (OSError, ssl.SSLError) as e:
            print(f"Impossible de charger le certificat ({e}). Lance d'abord : sh gen_cert.sh")
            return

    if socket.has_dualstack_ipv6():
        srv = socket.create_server(("::", args.port), family=socket.AF_INET6, dualstack_ipv6=True)
    else:
        srv = socket.create_server(("0.0.0.0", args.port))
    threading.Thread(target=reaper, daemon=True).start()
    mode = "TLS" if tls else "non chiffré"
    print(f"Faux serveur en écoute sur le port {args.port} ({mode}), sessions valables {TTL} s (Ctrl+C pour arrêter)")
    with srv:
        try:
            while True:
                conn, addr = srv.accept()  # socket d'écoute -> nouvelle socket client
                threading.Thread(target=serve, args=(conn, addr, tls), daemon=True).start()
        except KeyboardInterrupt:
            print("\nArrêt du serveur.")


if __name__ == "__main__":
    main()
