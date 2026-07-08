"""
Παρακάμπτει το DNS-μπλοκάρισμα του παρόχου (που δηλητηριάζει τα *.polymarket.com)
λύνοντας ΜΟΝΟ αυτά τα domains μέσω DNS-over-HTTPS στο Cloudflare (1.1.1.1).

Η επαλήθευση του TLS certificate ΠΑΡΑΜΕΝΕΙ ενεργή: αλλάζει μόνο ποια IP βρίσκουμε,
όχι το hostname/SNI/cert check. Δεν είναι VPN, δεν κρύβει την IP σου — απλώς βρίσκει
τη σωστή διεύθυνση αντί για τη σελίδα μπλοκαρίσματος.
"""

import socket
import ssl
import json
import urllib.request

_DOH_IP = "1.1.1.1"  # literal IP -> δεν χρειάζεται DNS για να ρωτήσουμε το DoH
_BLOCKED_SUFFIXES = (".polymarket.com",)
_cache: dict[str, str] = {}


def _doh_resolve(host: str) -> str:
    url = f"https://{_DOH_IP}/dns-query?name={host}&type=A"
    ctx = ssl.create_default_context()
    # το πιστοποιητικό στο 1.1.1.1 είναι για cloudflare-dns.com, όχι για την IP
    ctx.check_hostname = False
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        data = json.load(r)
    for ans in data.get("Answer", []):
        if ans.get("type") == 1:  # A record
            return ans["data"]
    raise RuntimeError(f"DoH: δεν βρέθηκε A record για {host}")


def install():
    """Κάνει monkeypatch το socket.getaddrinfo ώστε τα polymarket domains να λύνονται μέσω DoH."""
    _orig = socket.getaddrinfo

    def patched(host, port, family=0, type=0, proto=0, flags=0):
        if isinstance(host, str) and (host in ("polymarket.com", "www.polymarket.com")
                                       or host.endswith(_BLOCKED_SUFFIXES)):
            ip = _cache.get(host) or _doh_resolve(host)
            _cache[host] = ip
            return _orig(ip, port, family, type, proto, flags)
        return _orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched


if __name__ == "__main__":
    install()
    for h in ("gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com"):
        print(h, "->", _doh_resolve(h))
