# PgBouncer as a hosted Postgres runs it: transaction mode, TLS towards the client.
FROM alpine:3.20
RUN apk add --no-cache pgbouncer && adduser -D -H pooler
COPY pgbouncer.ini userlist.txt /etc/pgbouncer/
USER pooler
CMD ["pgbouncer", "/etc/pgbouncer/pgbouncer.ini"]
