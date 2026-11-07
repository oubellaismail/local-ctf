# Serves per-team zips for download challenges from the platform-data volume
# (mounted read-only at /data): /data/static/{challenge_id}/{team_id}.zip.
# controller-admin writes them (dashboard uploads and make challenges).
FROM nginxinc/nginx-unprivileged:1.27-alpine
RUN printf 'server {\n  listen 8080;\n  server_name _;\n  root /data/static;\n  autoindex off;\n  location / { try_files $uri =404; }\n}\n' \
    > /etc/nginx/conf.d/default.conf
