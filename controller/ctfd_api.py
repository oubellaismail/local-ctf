"""Small CTFd REST client for the admin pipeline (token auth)."""

from __future__ import annotations

import json

import requests

# CTFd config key holding {manifest_id: ctfd_challenge_id}. The plugin reads
# it to know which CTFd challenge is which manifest (downloads, Start
# button, visibility). Kept in CTFd's database, so backups include it.
CHALLENGE_MAP_KEY = "ctf_challenge_map"


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

    @staticmethod
    def _points(m: dict) -> tuple[str, dict]:
        """(CTFd challenge type, point fields) for manifest m."""
        if (m.get("scoring") or "dynamic") == "fixed":
            return "standard", {"value": int(m.get("value", 100))}
        initial = int(m.get("initial", 500))
        return "dynamic", {"initial": initial, "minimum": int(m.get("minimum", 50)),
                           "decay": int(m.get("decay", 15))}

    def upsert_challenge(self, m: dict, state_for_new: str = "hidden") -> tuple[str, int]:
        """Create or update the CTFd challenge for manifest m (matched by name).

        Returns ("created" | "updated", ctfd_id). New challenges start hidden
        unless state_for_new says otherwise; existing ones keep their state.
        Description, category and points are updated on existing ones.
        """
        name = m["name"]
        desc = m.get("description") or f"Challenge: {name}"
        if m.get("author"):
            desc += f"\n\n_Author: {m['author']}_"
        ctype, points = self._points(m)
        existing = self.challenges()
        if name in existing:
            cid = existing[name]["id"]
            if existing[name].get("type") not in (None, ctype):
                raise CTFdError(f"{name} is a {existing[name]['type']} challenge in CTFd; switching to "
                                f"{'fixed' if ctype == 'standard' else 'dynamic'} points needs "
                                "Delete and re-create")
            self._req("PATCH", f"/api/v1/challenges/{cid}",
                      json={"description": desc, "category": m["category"], **points})
            return "updated", cid
        body = {"name": name, "category": m["category"], "description": desc,
                "type": ctype, "state": state_for_new, **points}
        if ctype == "dynamic":
            body["value"] = points["initial"]
        data = self._req("POST", "/api/v1/challenges", json=body)
        return "created", data["data"]["id"]

    def challenge_map(self) -> dict:
        r = self.s.get(f"{self.url}/api/v1/configs/{CHALLENGE_MAP_KEY}", timeout=self.timeout)
        if r.status_code == 404:
            return {}
        if not r.ok:
            raise CTFdError(f"CTFd GET config: HTTP {r.status_code}")
        try:
            value = json.loads((r.json().get("data") or {}).get("value") or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def update_challenge_map(self, add: dict | None = None, remove: list | None = None) -> None:
        current = self.challenge_map()
        current.update({k: int(v) for k, v in (add or {}).items()})
        for k in remove or []:
            current.pop(k, None)
        self._req("PATCH", "/api/v1/configs", json={CHALLENGE_MAP_KEY: json.dumps(current, sort_keys=True)})

    def set_state(self, ctfd_id: int, state: str) -> None:
        if state not in ("hidden", "visible"):
            raise CTFdError(f"bad state {state}")
        self._req("PATCH", f"/api/v1/challenges/{ctfd_id}", json={"state": state})

    def delete_challenge(self, ctfd_id: int) -> None:
        self._req("DELETE", f"/api/v1/challenges/{ctfd_id}")
