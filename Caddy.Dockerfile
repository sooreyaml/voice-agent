FROM caddy:2-alpine

# Coolify can materialize relative bind sources as directories. Baking this
# static config into the image avoids a host-path mount while preserving runtime
# environment substitution inside the Caddyfile.
COPY Caddyfile /etc/caddy/Caddyfile
