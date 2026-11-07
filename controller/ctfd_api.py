"""Small CTFd REST client for the admin pipeline (token auth)."""

from __future__ import annotations

import requests


class CTFdError(RuntimeError):
    pass


class CTFd:
    def __init__(self, url: str, token: str, timeout: int = 20):
        if not token:
            raise CTFdError("No CTFd admin token. Run: make ctfd-setup")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        # Content-Type is required even on GETs, or CTFd ignores the token.
        self.s.headers.update({"Authorization": f"Token {token}",
                               "Content-Type": "application/json"})

    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{self.url}{path}", timeout=self.timeout, **kw)
        if not r.ok:
            raise CTFdError(f"CTFd {method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def _paged(self, path: str) -> list[dict]:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            body = self._req("GET", f"{path}{sep}per_page=100&page={page}")
            out += body.get("data", [])
            nxt = (body.get("meta") or {}).get("pagination", {}).get("next")
            if not nxt:
                return out
            page = nxt

    def challenges(self) -> dict[str, dict]:
        """{name: challenge} including hidden ones, with their state."""
        out = {}
        for c in self._paged("/api/v1/challenges?view=admin"):
            # The list endpoint doesn't include "state"; the detail one does
            if "state" not in c:
                c["state"] = self._req("GET", f"/api/v1/challenges/{c['id']}").get("data", {}).get("state")
            out[c["name"]] = c
        return out

    def team_ids(self) -> list[str]:
        return [str(t["id"]) for t in self._paged("/api/v1/teams")]

    def upsert_challenge(self, m: dict, state_for_new: str = "hidden") -> tuple[str, int]:
        """Create or update the CTFd challenge for manifest m (matched by name).

        Returns ("created" | "updated", ctfd_id). New challenges start hidden
        unless state_for_new says otherwise; existing ones keep their state.
        """
        name = m["name"]
        desc = m.get("description") or f"Challenge: {name}"
        if m.get("author"):
            desc += f"\n\n_Author: {m['author']}_"
        existing = self.challenges()
        if name in existing:
            cid = existing[name]["id"]
            self._req("PATCH", f"/api/v1/challenges/{cid}",
                      json={"description": desc, "category": m["category"]})
            return "updated", cid
        initial = int(m.get("initial", 500))
        body = {
            "name": name, "category": m["category"], "description": desc,
            "type": "dynamic", "value": initial, "initial": initial,
            "minimum": int(m.get("minimum", 50)), "decay": int(m.get("decay", 15)),
            "state": state_for_new,
        }
        data = self._req("POST", "/api/v1/challenges", json=body)
        return "created", data["data"]["id"]

    def set_state(self, ctfd_id: int, state: str) -> None:
        if state not in ("hidden", "visible"):
            raise CTFdError(f"bad state {state}")
        self._req("PATCH", f"/api/v1/challenges/{ctfd_id}", json={"state": state})

    def delete_challenge(self, ctfd_id: int) -> None:
        self._req("DELETE", f"/api/v1/challenges/{ctfd_id}")
