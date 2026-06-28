"""
Watch Vespa fill up in real time — run this in a SECOND terminal while
01_deploy_and_feed.py is feeding, to see the document count climb live.

    python scale_watch.py

Ctrl-C to stop. (Read-only; safe to start/stop anytime.)
"""

import time

import requests

SEARCH = "http://localhost:8080/search/"
HEALTH = "http://localhost:8080/state/v1/health"


def total_count():
    r = requests.get(
        SEARCH,
        params={"yql": "select * from sources * where true", "hits": 0, "timeout": "5s"},
        timeout=10,
    )
    return r.json()["root"]["fields"]["totalCount"]


def main():
    print("Polling http://localhost:8080 every 2s (Ctrl-C to stop)...\n")
    prev, t_prev, peak_rate = None, time.time(), 0.0
    while True:
        try:
            c = total_count()
        except Exception:  # noqa: BLE001
            print("   waiting for Vespa to answer queries...")
            time.sleep(2)
            continue
        now = time.time()
        rate = (c - prev) / (now - t_prev) if (prev is not None and now > t_prev) else 0.0
        peak_rate = max(peak_rate, rate)
        bar = "#" * min(60, c // 2000)  # one block per 2k docs, capped
        print(f"{time.strftime('%H:%M:%S')}  docs={c:>9,}  (+{rate:>6,.0f}/s, peak {peak_rate:,.0f}/s)  {bar}")
        prev, t_prev = c, now
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
