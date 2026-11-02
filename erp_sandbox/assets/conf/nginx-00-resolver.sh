#!/bin/sh
# Teach nginx to resolve upstream names at request time instead of at startup.
#
# Without this, nginx refuses to start when the bench container is not running:
# a name in an `upstream` block is resolved once, at config load, and a failure
# there is fatal. That is the wrong behaviour for a development environment,
# where the bench is stopped and restarted constantly and the developer needs
# the proxy to stay up and say 502 rather than vanish with it.
#
# The nameserver address cannot be hardcoded: Docker publishes an embedded DNS
# server at 127.0.0.11, podman runs aardvark-dns on the network's gateway. Both
# write it into the container's own /etc/resolv.conf, so read it from there.
#
# The official nginx image runs every executable in /docker-entrypoint.d before
# starting nginx; this is mounted in as one of them.

set -eu

target=/etc/nginx/conf.d/resolver.conf

servers="$(awk '/^nameserver/ { printf "%s ", $2 }' /etc/resolv.conf)"

if [ -z "$servers" ]; then
    echo "00-resolver.sh: no nameserver in /etc/resolv.conf; leaving nginx to resolve at startup" >&2
    exit 0
fi

# ipv6=off because an AAAA lookup for a container name that only has an A
# record produces a slow, confusing failure on some engines.
printf 'resolver %s valid=10s ipv6=off;\nresolver_timeout 5s;\n' "$servers" > "$target"

echo "00-resolver.sh: nginx will resolve upstreams via: $servers"
