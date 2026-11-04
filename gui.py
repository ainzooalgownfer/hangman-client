"""Interface graphique du client du pendu (tkinter).

Lancement : python3 gui.py      (Windows : py gui.py)
Le réseau (framing, request_id, reprise, messages poussés) vient de client.py,
qui doit être dans le même dossier.

Les requêtes partent dans un thread à part pour que la fenêtre ne gèle pas
pendant une reconnexion. Les messages poussés par le serveur (coup de
l'adversaire, déconnexion...) arrivent par le thread lecteur de la connexion ;
tout passe par une file que la fenêtre vide toutes les 50 ms, car seul le
thread principal a le droit de toucher à tkinter.
"""
import math
import queue
import ssl
import sys
import threading
import time

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    print("tkinter n'est pas installé pour ce Python.")
    print("Sur Mac avec Homebrew : brew install python-tk@3.15  (adapte au numéro de version de python3)")
    print("Sur Windows : réinstalle Python depuis python.org en cochant « tcl/tk and IDLE ».")
    sys.exit(1)

from client import (DEFAULT_CA, DEFAULT_PORT, Game, ServerTimeout, SessionLost,
                    base_letter, hangman_stage, make_tls_context)

# --- thème ---
BG, PANEL = "#14161f", "#1d2030"
FG, MUTED = "#e8e9f0", "#8a8fa8"
ACCENT, ACCENT_HOVER = "#5b7cfa", "#7a95ff"
GOOD, BAD, WARN = "#3fbf7f", "#e5535b", "#e8a33d"
KEY, KEY_HOVER = "#2a2e42", "#3a3f5a"
if sys.platform == "darwin":
    SANS, MONO = "Helvetica Neue", "Menlo"
elif sys.platform == "win32":
    SANS, MONO = "Segoe UI", "Consolas"
else:
    SANS, MONO = "DejaVu Sans", "DejaVu Sans Mono"
KEYBOARD = ("AZERTYUIOP", "QSDFGHJKLM", "WXCVBN")
ROOMS_REFRESH = 3      # secondes entre deux LIST_ROOMS sur l'écran des salles
HEARTBEAT = 45         # secondes sans requête avant un STATUS (sinon le serveur coupe pour inactivité)
STATUS_FR = {"WAITING": ("En attente", WARN), "PLAYING": ("En cours", ACCENT), "FINISHED": ("Terminée", MUTED)}


def fmt_time(secs):
    return f"⏱ {secs // 60}:{secs % 60:02d}"


class App:
    def __init__(self, root):
        self.root = root
        root.title("Pendu en réseau")
        root.geometry("840x660")
        root.minsize(780, 620)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Key>", self.on_key)

        self.events = queue.Queue()  # messages des threads réseau vers la fenêtre
        self.busy = False
        self.game = None
        self.screen = None
        self.status = None
        self.last_sent = time.monotonic()

        self.state = None           # partie solo : dernier STATE
        self.deadline = None        # chronomètre solo : instant local où le temps du serveur s'écoule
        self.status_asked = False   # STATUS déjà envoyé pour ce décompte

        self.room = None            # salle : dernière vue ROOM reçue
        self.room_max = 1           # essais au départ d'une partie en salle (pour le dessin)
        self.turn_deadline = None   # minuteur du tour en cours (affichage seulement)
        self.rooms_next = 0         # prochaine actualisation de la liste des salles

        self.show_login()
        self.after_ids = {"poll": root.after(50, self.poll), "tick": root.after(200, self.tick)}

    # ------------------------------------------------------------------ outils
    def clear(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.status = None

    def button(self, parent, text, command, primary=False, small=False, danger=False):
        """Bouton en Label : sur Mac, tk.Button ignore les couleurs de fond."""
        bg, hover = (ACCENT, ACCENT_HOVER) if primary else (KEY, KEY_HOVER)
        b = tk.Label(parent, text=text, bg=bg, fg="white" if primary else (BAD if danger else FG),
                     font=(SANS, 11 if small else 14, "bold"),
                     padx=14, pady=6 if small else 10, cursor="hand2")
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    def entry(self, parent, value="", width=26, size=14):
        e = tk.Entry(parent, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                     font=(SANS, size), highlightthickness=1,
                     highlightbackground=KEY, highlightcolor=ACCENT, width=width)
        e.insert(0, value)
        return e

    def field(self, parent, label, value):
        tk.Label(parent, text=label, fg=MUTED, bg=BG, font=(SANS, 11)).pack(anchor="w")
        e = self.entry(parent, value)
        e.pack(fill="x", ipady=6, pady=(2, 10))
        return e

    def checkbox(self, parent, text, var, size=10):
        return tk.Checkbutton(parent, text=text, variable=var, bg=BG, fg=MUTED, selectcolor=PANEL,
                              activebackground=BG, activeforeground=FG, highlightthickness=0,
                              font=(SANS, size))

    def top_bar(self, title):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=24, pady=(16, 0))
        tk.Label(top, text=title, fg=FG, bg=BG, font=(SANS, 13, "bold")).pack(side="left")
        self.status = tk.Label(top, text="", fg=MUTED, bg=BG, font=(SANS, 11))
        self.status.pack(side="right")
        return top

    def set_status(self, text, color=MUTED):
        if self.status is not None and self.status.winfo_exists():
            self.status.config(text=text, fg=color)

    def link_status(self, extra=""):
        g = self.game
        v = g.tls_version
        where = f"{g.host}:{g.port}"
        text = f"Chiffré ({v}) · {where}" if v else f"Non chiffré · {where}"
        self.set_status(text + (f" · {extra}" if extra else ""), GOOD if v else MUTED)

    def draw_hangman(self, canvas, stage, scale=1.0):
        """stage = étape du dessin, de 0 à 6 (déjà ramenée au nombre d'essais du serveur)."""
        c = canvas
        c.delete("all")
        col = BAD if stage >= 6 else FG
        w = max(2, round(5 * scale))
        c.create_line(30, 275, 230, 275, width=w, fill=MUTED, capstyle="round")  # sol
        if stage >= 1:
            c.create_line(70, 275, 70, 35, width=w, fill=col, capstyle="round")  # poteau
        if stage >= 2:
            c.create_line(70, 35, 175, 35, width=w, fill=col, capstyle="round")  # traverse
            c.create_line(70, 75, 110, 35, width=w, fill=col, capstyle="round")  # renfort
            c.create_line(175, 35, 175, 70, width=max(2, round(3 * scale)), fill=col)  # corde
        if stage >= 3:
            c.create_oval(155, 70, 195, 110, width=w, outline=col)               # tête
        if stage >= 4:
            c.create_line(175, 110, 175, 185, width=w, fill=col, capstyle="round")  # corps
        if stage >= 5:
            c.create_line(175, 130, 145, 160, width=w, fill=col, capstyle="round")  # bras
            c.create_line(175, 130, 205, 160, width=w, fill=col, capstyle="round")
        if stage >= 6:
            c.create_line(175, 185, 150, 235, width=w, fill=col, capstyle="round")  # jambes
            c.create_line(175, 185, 200, 235, width=w, fill=col, capstyle="round")
        if scale != 1.0:
            c.scale("all", 0, 0, scale, scale)

    def keyboard(self, parent, on_letter):
        kb = tk.Frame(parent, bg=BG)
        keys = {}
        for row in KEYBOARD:
            r = tk.Frame(kb, bg=BG)
            r.pack(pady=3)
            for ch in row:
                k = tk.Label(r, text=ch, width=3, font=(SANS, 15, "bold"),
                             bg=KEY, fg=FG, pady=8, cursor="hand2")
                k.pack(side="left", padx=3)
                k.bind("<Button-1>", lambda e, c=ch: on_letter(c))
                k.bind("<Enter>", lambda e, w=k: w.cget("cursor") == "hand2" and w.config(bg=KEY_HOVER))
                k.bind("<Leave>", lambda e, w=k: w.cget("cursor") == "hand2" and w.config(bg=KEY))
                keys[ch] = k
        return kb, keys

    def paint_keys(self, keys, masked, tried, active):
        # 'É' révélé dans le mot = la touche E est bonne (le serveur ignore les accents)
        revealed = {base_letter(c) for c in masked.split() if c != "_"}
        for ch, k in keys.items():
            if ch in tried:
                k.config(bg=GOOD if ch in revealed else BAD, fg="white", cursor="arrow")
            else:
                k.config(bg=KEY, fg=FG if active else MUTED, cursor="hand2" if active else "arrow")

    # ------------------------------------------------- réseau dans un thread
    def run(self, fn, done, *args):
        """Lance fn(*args) en arrière-plan ; done(résultat) est appelé dans la fenêtre."""
        if self.busy:
            return False
        self.set_busy(True)
        self.last_sent = time.monotonic()

        def worker():
            try:
                result = ("ok", fn(*args))
            except Exception as e:  # remonté à la fenêtre, traité dans on_failure
                result = ("err", e)
            self.events.put(("done", done, result))

        threading.Thread(target=worker, daemon=True).start()
        return True

    # Appelés depuis les threads réseau : ils ne font que remplir la file.
    def log(self, msg):
        self.events.put(("log", msg))

    def pushed(self, msg):
        self.events.put(("event", msg))

    def lost(self, conn):
        self.events.put(("disconnected", conn))

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self.set_status(ev[1].lstrip("[!it] ").strip(), WARN)
                elif kind == "event":
                    self.on_event(ev[1])
                elif kind == "disconnected":
                    self.on_disconnected(ev[1])
                else:
                    _, done, (status, value) = ev
                    self.set_busy(False)
                    if status == "ok":
                        done(value)
                    else:
                        self.on_failure(value)
        except queue.Empty:
            pass
        self.after_ids["poll"] = self.root.after(50, self.poll)

    def set_busy(self, busy):
        self.busy = busy
        self.root.config(cursor="watch" if busy else "")

    # ------------------------------------------------------------ connexion
    def show_login(self):
        self.clear()
        self.screen = "login"
        box = tk.Frame(self.root, bg=BG)
        box.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(box, text="PENDU", font=(SANS, 44, "bold"), fg=FG, bg=BG).pack()
        tk.Label(box, text="en réseau", font=(SANS, 15), fg=MUTED, bg=BG).pack(pady=(0, 26))
        self.e_host = self.field(box, "Serveur", "127.0.0.1")
        self.e_port = self.field(box, "Port", str(DEFAULT_PORT))
        self.e_name = self.field(box, "Pseudo", self.game.player if self.game else "")
        self.e_name.focus_set()
        self.use_tls = tk.BooleanVar(value=False)  # le serveur Go n'a pas (encore) de TLS
        self.checkbox(box, "Connexion chiffrée (TLS)", self.use_tls, size=12).pack(anchor="w")
        self.button(box, "Se connecter", self.submit, primary=True).pack(fill="x", pady=(14, 0))
        self.status = tk.Label(box, text="", fg=MUTED, bg=BG, font=(SANS, 11), wraplength=320)
        self.status.pack(pady=(14, 0))

    def submit(self):
        if self.busy:
            return
        host = self.e_host.get().strip() or "127.0.0.1"
        try:
            port = int(self.e_port.get())
        except ValueError:
            self.set_status("Port invalide.", BAD)
            return
        name = self.e_name.get().strip()
        if not name:
            self.set_status("Choisis un pseudo.", BAD)
            return

        tls = None
        if self.use_tls.get():
            try:
                tls = make_tls_context(DEFAULT_CA)
            except (OSError, ssl.SSLError):
                self.set_status("Certificat server.crt introuvable dans le dossier du client "
                                "(lance sh gen_cert.sh).", BAD)
                return

        if self.game:
            self.game.drop()
        self.game = Game(host, port, name, log=self.log, tls=tls,
                         on_event=self.pushed, on_disconnect=self.lost)
        self.set_status("Connexion…")
        if self.game.load():
            where = f"dans la salle {self.game.room_id}" if self.game.room_id else "en solo"
            if messagebox.askyesno("Partie en cours", f"{name}, tu as une partie en cours {where}.\nLa reprendre ?"):
                self.run(self.game.reconnect, self.on_resumed)
                return
            self.game.forget()
        self.run(self.game.reconnect, lambda _: self.show_menu())

    def on_resumed(self, resp):
        """Réponse au RESUME fait par reconnect() : on retourne là où on en était."""
        if resp is None:
            if self.screen == "login":
                self.show_menu()
            else:
                self.link_status("reconnecté")
        elif resp.get("type") == "ROOM":
            self.on_room(resp)
        elif resp.get("type") == "STATE":
            self.on_state(resp)

    def on_disconnected(self, conn):
        """Le thread lecteur a vu la connexion se fermer."""
        g = self.game
        if g is None or conn is not g.conn:
            return  # ancienne connexion, déjà remplacée
        if isinstance(conn.close_reason, ServerTimeout) and not g.room_id:
            self.set_status("Déconnecté pour inactivité : ta prochaine action te reconnecte.", WARN)
            return
        if self.busy:
            return  # la requête en cours se reconnecte elle-même
        if g.room_id or g.sid:
            self.set_status("Connexion perdue : reconnexion…", WARN)
            self.run(g.reconnect, self.on_resumed)

    def on_failure(self, e):
        g = self.game
        if isinstance(e, SessionLost):
            r = e.args[0]
            in_room = bool(g.room_id)
            g.forget()
            if in_room:
                messagebox.showwarning("Reprise impossible", f"[{r['code']}] {r['message']}")
                self.show_rooms()
            else:
                messagebox.showwarning("Reprise impossible",
                                       f"[{r['code']}] {r['message']}\nUne nouvelle partie va commencer.")
                self.new_game()
        elif isinstance(e, OSError):
            g.drop()
            text = str(e)
            if text.startswith("serveur injoignable"):
                msg = text[0].upper() + text[1:] + "."  # message déjà formé par client.py
            else:
                msg = f"Serveur injoignable ({text})."
            if "Connection refused" in text:
                msg += f"\nAucun serveur n'écoute sur le port {g.port} : vérifie qu'il est lancé et que le port est le bon."
            if self.screen == "login":
                self.set_status(msg, BAD)
            else:
                if g.sid or g.room_id:
                    msg += "\nTa partie est sauvegardée : ta prochaine action te reconnecte."
                self.set_status("Déconnecté", BAD)
                messagebox.showerror("Connexion", msg)
        else:
            messagebox.showerror("Erreur", repr(e))

    # ---------------------------------------------------------------- menu
    def show_menu(self):
        self.clear()
        self.screen = "menu"
        self.top_bar(f"Joueur : {self.game.player}")
        self.link_status()
        box = tk.Frame(self.root, bg=BG)
        box.place(relx=0.5, rely=0.48, anchor="center")
        tk.Label(box, text=f"Salut {self.game.player} !", font=(SANS, 30, "bold"), fg=FG, bg=BG).pack(pady=(0, 6))
        tk.Label(box, text="À quoi on joue ?", font=(SANS, 15), fg=MUTED, bg=BG).pack(pady=(0, 26))
        self.button(box, "Partie solo", self.new_game, primary=True).pack(fill="x", pady=6)
        self.button(box, "Duel en salle (2 joueurs)", self.show_rooms, primary=True).pack(fill="x", pady=6)
        self.button(box, "Changer de joueur", self.show_login, small=True).pack(fill="x", pady=(22, 0))

    # ------------------------------------------------------------ partie solo
    def new_game(self):
        self.run(self.game.new_game, self.on_state)

    def show_game(self):
        self.clear()
        self.screen = "game"
        self.top_bar(f"Joueur : {self.game.player} · solo")

        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill="both", expand=True, padx=24, pady=12)
        self.canvas = tk.Canvas(mid, width=260, height=300, bg=PANEL, highlightthickness=0)
        self.canvas.pack(side="left", anchor="n")

        right = tk.Frame(mid, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(28, 0))
        self.l_word = tk.Label(right, text="", font=(MONO, 34, "bold"), fg=FG, bg=BG)
        self.l_word.pack(anchor="w", pady=(34, 10))
        self.l_lives = tk.Label(right, text="", font=(SANS, 22), fg=BAD, bg=BG)
        self.l_lives.pack(anchor="w")
        self.l_timer = tk.Label(right, text="", font=(MONO, 22, "bold"), fg=FG, bg=BG)
        self.l_timer.pack(anchor="w", pady=(10, 0))
        self.l_msg = tk.Label(right, text="", font=(SANS, 15), fg=FG, bg=BG,
                              wraplength=420, justify="left")
        self.l_msg.pack(anchor="w", pady=(18, 0))
        self.end_box = tk.Frame(right, bg=BG)
        self.end_box.pack(anchor="w", pady=(16, 0))

        kb, self.keys = self.keyboard(self.root, self.play)
        kb.pack(pady=(0, 8))
        self.demo_bar(self.quit_game, "Abandonner")

    def demo_bar(self, quit_cmd, quit_text):
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=24, pady=(6, 16))
        self.button(bottom, quit_text, quit_cmd, small=True, danger=True).pack(side="left")
        demo = tk.Frame(bottom, bg=BG)
        demo.pack(side="right")
        tk.Label(demo, text="Tests démo :", fg=MUTED, bg=BG, font=(SANS, 10)).pack(side="left", padx=(0, 8))
        self.button(demo, "Couper la connexion", self.cut, small=True).pack(side="left", padx=(0, 8))
        self.lose_next = tk.BooleanVar(value=False)
        self.checkbox(demo, "Perdre la prochaine réponse", self.lose_next).pack(side="left")

    def render(self, extra=None):
        s = self.state
        over = s["status"] != "IN_PROGRESS"
        left = s["attempts_left"]
        total = max(self.game.max_attempts, left)
        self.l_word.config(text=s["masked_word"])
        self.l_lives.config(text="♥" * left + "♡" * (total - left))
        self.draw_hangman(self.canvas, self.game.stage(s))

        # Chronomètre : le serveur donne le temps restant, on en déduit l'échéance locale.
        # Ce décompte n'est qu'un affichage : c'est le serveur qui décide de la fin.
        ms = s.get("time_left_ms")
        if ms is not None and not over:
            self.deadline = time.monotonic() + ms / 1000
            self.status_asked = False
        else:
            self.deadline = None
        if s.get("reason") == "TIME_UP":
            self.l_timer.config(text=fmt_time(0), fg=BAD)
        elif ms is None or over:
            self.l_timer.config(text="")

        self.paint_keys(self.keys, s["masked_word"], set(s["tried_letters"]), not over)

        msg = s["message"]
        if s["status"] == "LOST" and s.get("secret_word"):
            msg += f"  Le mot était {s['secret_word']}."
        if s.get("replayed"):
            msg += "\nRéponse renvoyée par le serveur : le coup n'a pas été compté deux fois."
        if extra:
            msg = extra
        color = {"WON": GOOD, "LOST": BAD, "ABANDONED": MUTED}.get(s["status"], FG)
        self.l_msg.config(text=msg, fg=color)

        for w in self.end_box.winfo_children():
            w.destroy()
        if over:
            self.button(self.end_box, "Rejouer", self.new_game, primary=True).pack(side="left")
            self.button(self.end_box, "Menu", self.show_menu).pack(side="left", padx=(10, 0))
        self.link_status(f"session {s['session_id'][:8]}")
        self.tick(reschedule=False)

    def play(self, letter):
        if self.screen != "game" or self.busy or not self.state:
            return
        if self.state["status"] != "IN_PROGRESS" or letter in self.state["tried_letters"]:
            return
        msg = {"action": "LETTER", "session_id": self.game.sid, "letter": letter}
        if self.lose_next.get():
            self.lose_next.set(False)
            self.run(self.game.lost_response, self.on_state, msg)
        else:
            self.run(self.game.request, self.on_state, msg)

    def quit_game(self):
        if self.busy or not self.state or self.state["status"] != "IN_PROGRESS":
            return
        if messagebox.askyesno("Abandonner", "Abandonner la partie ?"):
            self.run(self.game.request, self.on_bye, {"action": "QUIT", "session_id": self.game.sid})

    def cut(self):
        if self.busy or not self.game or not self.game.conn:
            return
        self.game.cut()
        self.set_status("Connexion coupée (test).", WARN)

    def on_state(self, resp):
        if resp is None:
            return
        if resp.get("type") == "ERROR":
            text = f"[{resp['code']}] {resp['message']}"
            if self.screen == "game":
                self.l_msg.config(text=text, fg=BAD)
            else:
                self.set_status(text, BAD)
            return
        if resp.get("type") != "STATE":
            return
        if self.screen != "game":
            self.show_game()
        self.state = resp
        self.render()
        if resp["status"] != "IN_PROGRESS":
            self.game.forget()

    def on_bye(self, resp):
        if resp.get("type") == "STATE":  # trop tard pour abandonner : perdu au temps
            self.on_state(resp)
            return
        if resp.get("type") == "ERROR":
            self.l_msg.config(text=f"[{resp['code']}] {resp['message']}", fg=BAD)
            return
        self.state = dict(self.state, status="ABANDONED")
        self.game.forget()
        self.render(extra=f"Partie abandonnée. Le mot était {resp.get('secret_word', '?')}.")

    # ---------------------------------------------------------- liste des salles
    def show_rooms(self):
        self.clear()
        self.screen = "rooms"
        self.room = None
        top = self.top_bar(f"Joueur : {self.game.player} · salles")
        self.link_status()

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=24, pady=(18, 4))
        tk.Label(head, text="Salles de jeu", font=(SANS, 26, "bold"), fg=FG, bg=BG).pack(side="left")
        self.button(head, "Menu", self.show_menu, small=True).pack(side="right")
        self.button(head, "Actualiser", self.refresh_rooms, small=True).pack(side="right", padx=(0, 8))
        tk.Label(self.root, text="Rejoins une salle en attente, ou crée la tienne et attends un adversaire.",
                 font=(SANS, 12), fg=MUTED, bg=BG).pack(anchor="w", padx=24)

        create = tk.Frame(self.root, bg=PANEL, padx=14, pady=12)
        create.pack(fill="x", padx=24, pady=(14, 10))
        tk.Label(create, text="Nouvelle salle", font=(SANS, 12, "bold"), fg=FG, bg=PANEL).pack(side="left")
        self.e_room = self.entry(create, f"Salle de {self.game.player}", width=24, size=13)
        self.e_room.pack(side="left", padx=12, ipady=4)
        self.button(create, "Créer la salle", self.create_room, primary=True, small=True).pack(side="left")

        self.rooms_box = tk.Frame(self.root, bg=BG)
        self.rooms_box.pack(fill="both", expand=True, padx=24, pady=(4, 16))
        tk.Label(self.rooms_box, text="Chargement…", fg=MUTED, bg=BG, font=(SANS, 12)).pack(anchor="w")
        self.rooms_next = 0
        self.refresh_rooms()

    def refresh_rooms(self):
        self.rooms_next = time.monotonic() + ROOMS_REFRESH
        self.run(self.game.list_rooms, self.render_rooms)

    def render_rooms(self, resp):
        if self.screen != "rooms":
            return
        if resp.get("type") == "ERROR":
            self.set_status(f"[{resp['code']}] {resp['message']}", BAD)
            return
        for w in self.rooms_box.winfo_children():
            w.destroy()
        rooms = resp.get("rooms") or []
        if not rooms:
            tk.Label(self.rooms_box, text="Aucune salle pour l'instant : crée la première !",
                     fg=MUTED, bg=BG, font=(SANS, 13)).pack(anchor="w", pady=10)
            return
        for r in rooms[:8]:
            row = tk.Frame(self.rooms_box, bg=PANEL, padx=14, pady=10)
            row.pack(fill="x", pady=4)
            info = tk.Frame(row, bg=PANEL)
            info.pack(side="left", fill="x", expand=True)
            tk.Label(info, text=r["name"], font=(SANS, 14, "bold"), fg=FG, bg=PANEL).pack(anchor="w")
            players = r["players"] + ["—"] * (2 - len(r["players"]))
            tk.Label(info, text=f"#{r['room_id']}  ·  {players[0]} contre {players[1]}",
                     font=(SANS, 11), fg=MUTED, bg=PANEL).pack(anchor="w")
            label, color = STATUS_FR.get(r["status"], (r["status"], MUTED))
            if r["status"] == "WAITING":
                self.button(row, "Rejoindre", lambda rid=r["room_id"]: self.join_room(rid),
                            primary=True, small=True).pack(side="right")
            tk.Label(row, text=label, font=(SANS, 11, "bold"), fg=color, bg=PANEL).pack(side="right", padx=12)
        if len(rooms) > 8:
            tk.Label(self.rooms_box, text=f"… et {len(rooms) - 8} autre(s).", fg=MUTED, bg=BG,
                     font=(SANS, 11)).pack(anchor="w")

    def create_room(self):
        if self.screen == "rooms":
            self.run(self.game.create_room, self.on_room, self.e_room.get().strip())

    def join_room(self, room_id):
        self.run(self.game.join_room, self.on_room, room_id)

    # ------------------------------------------------------------ salle : attente et duel
    def on_event(self, msg):
        """Message poussé par le serveur (sans request_id)."""
        if msg.get("type") == "ROOM" and self.game and msg.get("room_id") == self.game.room_id:
            self.on_room(msg)

    def on_room(self, resp):
        """Réponse à une requête de salle, ou message poussé : on affiche la salle."""
        if resp is None:
            return
        t = resp.get("type")
        if t == "ERROR":
            text = f"[{resp['code']}] {resp['message']}"
            if self.screen == "duel":
                self.l_turn_msg.config(text=text, fg=BAD)
            else:
                self.set_status(text, BAD)
            if resp.get("code") in ("UNKNOWN_ROOM", "PLAYER_NOT_ALLOWED") and self.screen in ("waiting", "duel"):
                self.game.forget()
                messagebox.showwarning("Salle", resp["message"])
                self.show_rooms()
            return
        if t != "ROOM":
            return
        if self.room and self.room.get("room_id") == resp["room_id"] and resp.get("version", 0) < self.room.get("version", 0):
            return  # état plus ancien que celui déjà affiché (réponse rejouée, message en retard)
        if not self.room or self.room.get("room_id") != resp["room_id"]:
            self.room_max = 1
        self.room = resp
        for p in resp.get("players") or []:
            self.room_max = max(self.room_max, p["attempts_left"])
        if resp["status"] == "WAITING":
            if self.screen != "waiting":
                self.show_waiting()
            self.render_waiting()
        else:
            if self.screen != "duel":
                self.show_duel()
            self.render_duel()

    def show_waiting(self):
        self.clear()
        self.screen = "waiting"
        self.top_bar(f"Joueur : {self.game.player} · salle")
        box = tk.Frame(self.root, bg=BG)
        box.place(relx=0.5, rely=0.46, anchor="center")
        self.l_wait_name = tk.Label(box, text="", font=(SANS, 28, "bold"), fg=FG, bg=BG)
        self.l_wait_name.pack()
        self.l_wait_code = tk.Label(box, text="", font=(MONO, 18), fg=ACCENT, bg=BG)
        self.l_wait_code.pack(pady=(6, 26))
        self.l_wait = tk.Label(box, text="En attente d'un adversaire", font=(SANS, 16), fg=MUTED, bg=BG)
        self.l_wait.pack()
        tk.Label(box, text="La partie démarre dès qu'un autre joueur rejoint la salle.",
                 font=(SANS, 12), fg=MUTED, bg=BG).pack(pady=(6, 28))
        self.button(box, "Quitter la salle", self.leave_room, danger=True).pack()

    def render_waiting(self):
        r = self.room
        self.l_wait_name.config(text=r["room_name"])
        self.l_wait_code.config(text=f"Code de la salle : {r['room_id']}")
        self.link_status(f"salle {r['room_id']}")

    def show_duel(self):
        self.clear()
        self.screen = "duel"
        r = self.room
        self.top_bar(f"{r['room_name']} · #{r['room_id']}")

        cards = tk.Frame(self.root, bg=BG)
        cards.pack(fill="x", padx=24, pady=(12, 4))
        self.cards = []
        for i in range(2):
            card = tk.Frame(cards, bg=PANEL, highlightthickness=2, highlightbackground=PANEL, padx=10, pady=8)
            card.pack(side="left", fill="both", expand=True, padx=(0, 10) if i == 0 else (10, 0))
            canvas = tk.Canvas(card, width=104, height=120, bg=PANEL, highlightthickness=0)
            canvas.pack(side="left")
            info = tk.Frame(card, bg=PANEL)
            info.pack(side="left", fill="both", expand=True, padx=(10, 0))
            name = tk.Label(info, text="", font=(SANS, 15, "bold"), fg=FG, bg=PANEL)
            name.pack(anchor="w")
            score = tk.Label(info, text="", font=(SANS, 13), fg=FG, bg=PANEL)
            score.pack(anchor="w", pady=(4, 0))
            lives = tk.Label(info, text="", font=(SANS, 15), fg=BAD, bg=PANEL)
            lives.pack(anchor="w")
            link = tk.Label(info, text="", font=(SANS, 10), fg=MUTED, bg=PANEL)
            link.pack(anchor="w", pady=(4, 0))
            self.cards.append({"frame": card, "canvas": canvas, "name": name, "score": score,
                               "lives": lives, "link": link})

        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill="x", padx=24, pady=(10, 0))
        self.l_word = tk.Label(mid, text="", font=(MONO, 32, "bold"), fg=FG, bg=BG)
        self.l_word.pack()
        line = tk.Frame(mid, bg=BG)
        line.pack(pady=(6, 0))
        self.l_turn = tk.Label(line, text="", font=(SANS, 16, "bold"), fg=FG, bg=BG)
        self.l_turn.pack(side="left")
        self.l_turn_timer = tk.Label(line, text="", font=(MONO, 16, "bold"), fg=FG, bg=BG)
        self.l_turn_timer.pack(side="left", padx=(14, 0))
        self.l_turn_msg = tk.Label(mid, text="", font=(SANS, 13), fg=MUTED, bg=BG, wraplength=760)
        self.l_turn_msg.pack(pady=(4, 0))
        self.end_box = tk.Frame(mid, bg=BG)
        self.end_box.pack(pady=(6, 0))

        kb, self.keys = self.keyboard(self.root, self.play_room)
        kb.pack(pady=(6, 4))
        self.demo_bar(self.leave_room, "Quitter la salle")

    def render_duel(self):
        r = self.room
        me = r["you"]
        players = r["players"]
        playing = r["status"] == "PLAYING"
        turn = r.get("turn")
        my_turn = playing and turn == me

        for i, card in enumerate(self.cards):
            if i >= len(players):
                continue
            p = players[i]
            on_turn = playing and turn == i
            card["frame"].config(highlightbackground=ACCENT if on_turn else PANEL)
            card["name"].config(text=p["name"] + ("  (toi)" if i == me else ""))
            card["score"].config(text=f"Score : {p['score']}")
            left = p["attempts_left"]
            card["lives"].config(text="♥" * left + "♡" * max(0, self.room_max - left))
            if p["connected"]:
                card["link"].config(text="● connecté", fg=GOOD)
            else:
                card["link"].config(text="○ déconnecté…", fg=WARN)
            self.draw_hangman(card["canvas"], hangman_stage(left, self.room_max), scale=0.4)

        self.l_word.config(text=r.get("masked_word", ""))
        tried = set(r.get("tried_letters") or [])
        self.paint_keys(self.keys, r.get("masked_word", ""), tried, my_turn)

        ms = r.get("turn_time_left_ms")
        self.turn_deadline = time.monotonic() + ms / 1000 if (playing and ms is not None) else None
        if not self.turn_deadline:
            self.l_turn_timer.config(text="")

        for w in self.end_box.winfo_children():
            w.destroy()
        if playing:
            if my_turn:
                self.l_turn.config(text="À toi de jouer !", fg=GOOD)
            else:
                self.l_turn.config(text=f"Au tour de {players[turn]['name']}…", fg=MUTED)
            self.l_turn_msg.config(text=r.get("message", ""), fg=MUTED)
        else:
            result = r.get("result")
            title, color = {"WIN": ("Victoire !", GOOD), "LOSE": ("Défaite", BAD)}.get(result, ("Égalité", WARN))
            self.l_turn.config(text=title, fg=color)
            self.l_turn_msg.config(text=f"{r.get('message', '')}  Le mot était {r.get('secret_word', '?')}.", fg=FG)
            self.button(self.end_box, "Retour aux salles", self.leave_room, primary=True, small=True).pack()
        if r.get("replayed"):
            self.l_turn_msg.config(text=self.l_turn_msg.cget("text") +
                                   "\nRéponse renvoyée par le serveur : le coup n'a pas été compté deux fois.")
        self.link_status(f"salle {r['room_id']}")
        self.tick(reschedule=False)

    def play_room(self, letter):
        r = self.room
        if self.screen != "duel" or self.busy or not r or r["status"] != "PLAYING":
            return
        if r.get("turn") != r["you"] or letter in (r.get("tried_letters") or []):
            return
        if self.lose_next.get():
            self.lose_next.set(False)
            msg = {"action": "ROOM_LETTER", "room_id": self.game.room_id, "letter": letter}
            self.run(self.game.lost_response, self.on_room, msg)
        else:
            self.run(self.game.room_letter, self.on_room, letter)

    def leave_room(self):
        if self.busy or not self.game.room_id:
            return
        r = self.room
        if r and r["status"] == "PLAYING" and not messagebox.askyesno(
                "Quitter la salle", "La partie est en cours : la quitter te fait perdre par forfait. Quitter ?"):
            return
        self.run(self.game.leave_room, lambda resp: self.show_rooms())

    # --------------------------------------------------------- minuteurs
    def tick(self, reschedule=True):
        now = time.monotonic()
        # Partie solo : décompte du chronomètre ; à zéro, on demande le verdict au serveur (STATUS).
        if self.screen == "game" and self.deadline is not None:
            left = self.deadline - now
            secs = max(0, math.ceil(left))
            color = BAD if secs <= 10 else WARN if secs <= 30 else FG
            self.l_timer.config(text=fmt_time(secs), fg=color)
            if left <= 0 and not self.busy and not self.status_asked:
                self.status_asked = True
                self.run(self.game.request, self.on_state,
                         {"action": "STATUS", "session_id": self.game.sid})
        # Duel : temps restant pour le tour (c'est le serveur qui fait passer le tour).
        if self.screen == "duel" and self.turn_deadline is not None:
            secs = max(0, math.ceil(self.turn_deadline - now))
            self.l_turn_timer.config(text=fmt_time(secs), fg=BAD if secs <= 5 else WARN if secs <= 10 else MUTED)
        # Attente : petits points qui bougent.
        if self.screen == "waiting":
            self.l_wait.config(text="En attente d'un adversaire" + "." * (int(now * 2) % 4))
        # Liste des salles : actualisation régulière.
        if self.screen == "rooms" and not self.busy and now >= self.rooms_next:
            self.refresh_rooms()
        # Salle : un STATUS de temps en temps, sinon le serveur coupe une connexion inactive.
        if (self.screen in ("waiting", "duel") and not self.busy and self.game and self.game.room_id
                and now - self.last_sent > HEARTBEAT):
            self.run(self.game.room_status, self.on_room)
        if reschedule:
            self.after_ids["tick"] = self.root.after(200, self.tick)

    # ------------------------------------------------------------- clavier
    def on_key(self, event):
        if isinstance(event.widget, tk.Entry):
            if event.keysym in ("Return", "KP_Enter"):
                if self.screen == "login":
                    self.submit()
                elif self.screen == "rooms":
                    self.create_room()
            return  # on écrit dans un champ : ce n'est pas une lettre jouée
        ch = event.char
        if len(ch) == 1 and ch.isalpha():
            if self.screen == "game":
                self.play(ch.upper())
            elif self.screen == "duel":
                self.play_room(ch.upper())

    def on_close(self):
        if self.game:
            self.game.drop()  # la partie reste sauvegardée : on pourra la reprendre
        for after_id in self.after_ids.values():
            self.root.after_cancel(after_id)
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
