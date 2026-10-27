#!/bin/sh
# Génère un certificat auto-signé pour le serveur du pendu (valable 1 an).
# Il couvre localhost, 127.0.0.1 et ::1.
#   server.key : clé privée, reste sur la machine du serveur (ne pas la partager)
#   server.crt : certificat public, à donner aux clients pour qu'ils vérifient le serveur
# Le serveur Go peut utiliser les mêmes fichiers : tls.LoadX509KeyPair("server.crt", "server.key")
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout server.key -out server.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1"
echo "Certificat créé : server.crt (public) et server.key (privée)"
