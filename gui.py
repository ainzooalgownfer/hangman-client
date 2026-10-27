"""Interface graphique du client du pendu (tkinter).

Lancement : python3 gui.py
Le réseau (framing, request_id, reprise de session) vient de client.py, qui doit
être dans le même dossier. Les requêtes partent dans un thread à part pour que
la fenêtre ne gèle pas pendant une reconnexion.
"""
import queue
import sys
import threading

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    print("tkinter n'est pas installé pour ce Python.")
    print("Sur Mac avec Homebrew : brew install python-tk@3.15  (adapte au numéro de version de python3)")
    sys.exit(1)

import ssl

from client import DEFAULT_CA, Game, SessionLost, make_tls_context

# --- thème ---
BG, PANEL = "#14161f", "#1d2030"
FG, MUTED = "#e8e9f0", "#8a8fa8"
ACCENT, ACCENT_HOVER = "#5b7cfa", "#7a95ff"
GOOD, BAD, WARN = "#3fbf7f", "#e5535b", "#e8a33d"
KEY, KEY_HOVER = "#2a2e42", "#3a3f5a"
SANS, MONO = "Helvetica Neue", "Menlo"
KEYBOARD = ("AZERTYUIOP", "QSDFGHJKLM", "WXCVBN")


class App:
    def __init__(self, root):
        self.root = root
        root.title("Pendu en réseau")
        root.geometry("780x600")
        root.minsize(720, 560)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Key>", self.on_key)

        self.events = queue.Queue()  # messages du thread réseau vers la fenêtre
        self.busy = False
        self.game = None
        self.state = None
        self.screen = None
        self.status = None

        self.show_login()
        root.after(50, self.poll)

    # ------------------------------------------------------------------ outils
    def clear(self):
        for w in self.root.winfo_children():
            w.destroy()

    def button(self, parent, text, command, primary=False, small=False):
        """Bouton en Label : sur Mac, tk.Button ignore les couleurs de fond."""
        bg, hover = (ACCENT, ACCENT_HOVER) if primary else (KEY, KEY_HOVER)
        b = tk.Label(parent, text=text, bg=bg, fg="white" if primary else FG,
                     font=(SANS, 11 if small else 14, "bold"),
                     padx=14, pady=6 if small else 10, cursor="hand2")
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    def field(self, parent, label, value):
        tk.Label(parent, text=label, fg=MUTED, bg=BG, font=(SANS, 11)).pack(anchor="w")
        e = tk.Entry(parent, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                     font=(SANS, 14), highlightthickness=1,
                     highlightbackground=KEY, highlightcolor=ACCENT, width=26)
        e.insert(0, value)
        e.pack(fill="x", ipady=6, pady=(2, 10))
        return e

    def set_status(self, text, color=MUTED):
        if self.status is not None and self.status.winfo_exists():
            self.status.config(text=text, fg=color)

    # ------------------------------------------------- réseau dans un thread
    def run(self, fn, done, *args):
        """Lance fn(*args) en arrière-plan ; done(résultat) est appelé dans la fenêtre."""
        if self.busy:
            return
        self.set_busy(True)

        def worker():
            try:
                result = ("ok", fn(*args))
            except Exception as e:  # remonté à la fenêtre, traité dans on_failure
                result = ("err", e)
            self.events.put(("done", done, result))

        threading.Thread(target=worker, daemon=True).start()

    def log(self, msg):
        """Appelé par Game depuis le thread réseau : passe par la file, jamais directement par tk."""
        self.events.put(("log", msg))

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "log":
                    self.set_status(ev[1].lstrip("[!it] ").strip(), WARN)
                else:
                    _, done, (kind, value) = ev
                    self.set_busy(False)
                    if kind == "ok":
                        done(value)
                    else:
                        self.on_failure(value)
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

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
        self.e_port = self.field(box, "Port", "5050")
        self.e_name = self.field(box, "Pseudo", "")
        self.e_name.focus_set()
        self.use_tls = tk.BooleanVar(value=DEFAULT_CA.exists())
        tk.Checkbutton(box, text="Connexion chiffrée (TLS)", variable=self.use_tls,
                       bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                       activeforeground=FG, highlightthickness=0,
                       font=(SANS, 12)).pack(anchor="w")
        self.button(box, "Jouer", self.submit, primary=True).pack(fill="x", pady=(14, 0))
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

        self.game = Game(host, port, name, log=self.log, tls=tls)
        self.set_status("Connexion…")
        if self.game.load() and messagebox.askyesno(
                "Partie en cours", f"{name}, tu as une partie en cours.\nLa reprendre ?"):
            self.run(self.game.attach, self.on_state)
        else:
            self.game.forget()
            self.new_game()

    def new_game(self):
        self.run(self.game.new_game, self.on_state)

    # ----------------------------------------------------------------- jeu
    def show_game(self):
        self.clear()
        self.screen = "game"

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=24, pady=(16, 0))
        tk.Label(top, text=f"Joueur : {self.game.player}", fg=FG, bg=BG,
                 font=(SANS, 13, "bold")).pack(side="left")
        self.status = tk.Label(top, text="", fg=MUTED, bg=BG, font=(SANS, 11))
        self.status.pack(side="right")

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
        self.l_msg = tk.Label(right, text="", font=(SANS, 15), fg=FG, bg=BG,
                              wraplength=400, justify="left")
        self.l_msg.pack(anchor="w", pady=(18, 0))
        self.end_box = tk.Frame(right, bg=BG)
        self.end_box.pack(anchor="w", pady=(16, 0))

        kb = tk.Frame(self.root, bg=BG)
        kb.pack(pady=(0, 8))
        self.keys = {}
        for row in KEYBOARD:
            r = tk.Frame(kb, bg=BG)
            r.pack(pady=3)
            for ch in row:
                k = tk.Label(r, text=ch, width=3, font=(SANS, 15, "bold"),
                             bg=KEY, fg=FG, pady=8, cursor="hand2")
                k.pack(side="left", padx=3)
                k.bind("<Button-1>", lambda e, c=ch: self.play(c))
                k.bind("<Enter>", lambda e, w=k: w.cget("cursor") == "hand2" and w.config(bg=KEY_HOVER))
                k.bind("<Leave>", lambda e, w=k: w.cget("cursor") == "hand2" and w.config(bg=KEY))
                self.keys[ch] = k

        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=24, pady=(6, 16))
        self.button(bottom, "Abandonner", self.quit_game, small=True).pack(side="left")
        demo = tk.Frame(bottom, bg=BG)
        demo.pack(side="right")
        tk.Label(demo, text="Tests démo :", fg=MUTED, bg=BG, font=(SANS, 10)).pack(side="left", padx=(0, 8))
        self.button(demo, "Couper la connexion", self.cut, small=True).pack(side="left", padx=(0, 8))
        self.lose_next = tk.BooleanVar(value=False)
        tk.Checkbutton(demo, text="Perdre la prochaine réponse", variable=self.lose_next,
                       bg=BG, fg=MUTED, selectcolor=PANEL, activebackground=BG,
                       activeforeground=FG, highlightthickness=0,
                       font=(SANS, 10)).pack(side="left")

    def draw_hangman(self, errors):
        c = self.canvas
        c.delete("all")
        col = BAD if errors >= 6 else FG
        w = 5
        c.create_line(30, 275, 230, 275, width=w, fill=MUTED, capstyle="round")  # sol
        if errors >= 1:
            c.create_line(70, 275, 70, 35, width=w, fill=col, capstyle="round")  # poteau
        if errors >= 2:
            c.create_line(70, 35, 175, 35, width=w, fill=col, capstyle="round")  # traverse
            c.create_line(70, 75, 110, 35, width=w, fill=col, capstyle="round")  # renfort
            c.create_line(175, 35, 175, 70, width=3, fill=col)                   # corde
        if errors >= 3:
            c.create_oval(155, 70, 195, 110, width=w, outline=col)               # tête
        if errors >= 4:
            c.create_line(175, 110, 175, 185, width=w, fill=col, capstyle="round")  # corps
        if errors >= 5:
            c.create_line(175, 130, 145, 160, width=w, fill=col, capstyle="round")  # bras
            c.create_line(175, 130, 205, 160, width=w, fill=col, capstyle="round")
        if errors >= 6:
            c.create_line(175, 185, 150, 235, width=w, fill=col, capstyle="round")  # jambes
            c.create_line(175, 185, 200, 235, width=w, fill=col, capstyle="round")

    def render(self, extra=None):
        s = self.state
        over = s["status"] != "IN_PROGRESS"
        left = s["attempts_left"]
        self.l_word.config(text=s["masked_word"])
        self.l_lives.config(text="♥" * left + "♡" * (6 - left))
        self.draw_hangman(6 - left)

        revealed = set(s["masked_word"].split())
        tried = set(s["tried_letters"])
        for ch, k in self.keys.items():
            if ch in tried:
                k.config(bg=GOOD if ch in revealed else BAD, fg="white", cursor="arrow")
            else:
                k.config(bg=KEY, fg=MUTED if over else FG, cursor="arrow" if over else "hand2")

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
        v = self.game.tls_version
        if v:
            self.set_status(f"Chiffré ({v}) · {self.game.host}:{self.game.port} · session {s['session_id'][:8]}", GOOD)
        else:
            self.set_status(f"Non chiffré · {self.game.host}:{self.game.port} · session {s['session_id'][:8]}")

    # ------------------------------------------------------------ actions
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
        self.set_status("Connexion coupée (test) : joue une lettre pour voir la reconnexion.", WARN)

    # ---------------------------------------------------------- réponses
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
        if self.screen != "game":
            self.show_game()
        self.state = resp
        self.render()
        if resp["status"] != "IN_PROGRESS":
            self.game.forget()

    def on_bye(self, resp):
        if resp.get("type") == "ERROR":
            self.l_msg.config(text=f"[{resp['code']}] {resp['message']}", fg=BAD)
            return
        self.state = dict(self.state, status="ABANDONED")
        self.game.forget()
        self.render(extra=f"Partie abandonnée. Le mot était {resp.get('secret_word', '?')}.")

    def on_failure(self, e):
        if isinstance(e, SessionLost):
            r = e.args[0]
            self.game.forget()
            messagebox.showwarning("Reprise impossible",
                                   f"[{r['code']}] {r['message']}\nUne nouvelle partie va commencer.")
            self.new_game()
        elif isinstance(e, OSError):
            self.game.drop()
            msg = f"Serveur injoignable ({e})."
            if self.screen == "login":
                self.set_status(msg, BAD)
            else:
                if self.game.sid:
                    msg += "\nTa partie est sauvegardée : rejoue une lettre pour réessayer."
                self.set_status("Déconnecté", BAD)
                messagebox.showerror("Connexion", msg)
        else:
            messagebox.showerror("Erreur", repr(e))

    # ------------------------------------------------------------- clavier
    def on_key(self, event):
        if self.screen == "login":
            if event.keysym in ("Return", "KP_Enter"):
                self.submit()
            return
        ch = event.char
        if len(ch) == 1 and ch.isalpha():
            self.play(ch.upper())

    def on_close(self):
        if self.game:
            self.game.drop()  # la session reste sauvegardée : on pourra la reprendre
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
