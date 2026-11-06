# CTFd + the controller plugin (Start/Stop instance buttons, per-team flags,
# per-team downloads, branding). Build context is the repo root.
#   make build-ctfd
ARG CTFD_VERSION=latest
FROM ctfd/ctfd:${CTFD_VERSION}
USER root
COPY ctfd-plugin/ /opt/CTFd/CTFd/plugins/controller-plugin/
RUN chown -R 1001:1001 /opt/CTFd/CTFd/plugins/controller-plugin \
 && find /opt/CTFd/CTFd/plugins/controller-plugin -name '__pycache__' -prune -exec rm -rf {} + \
 && mkdir -p /var/uploads && chown 1001:1001 /var/uploads
USER 1001
