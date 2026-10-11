#!/bin/sh
# A certificate authority of the stand-in's own, and one server certificate for its three services.
# Made once, into the `certs` volume. Nothing here is a secret worth keeping: it never leaves the machine.
set -e
cd /certs
[ -f ca.crt ] && [ -f server.crt ] && exit 0
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=tripplanner prod-like stand-in CA" \
  -keyout ca.key -out ca.crt 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=stand-in" -keyout server.key -out server.csr 2>/dev/null
printf "subjectAltName=DNS:neon-standin,DNS:pooler,DNS:upstash-standin,DNS:localhost,IP:127.0.0.1\n" > san.cnf
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 365 -extfile san.cnf \
  -out server.crt 2>/dev/null
# Postgres will not start on a key that anyone else can read; the other two do not mind.
cp server.key postgres.key
chown 999:999 postgres.key
chmod 600 postgres.key
chmod 644 server.key server.crt ca.crt
echo "certificates made"
