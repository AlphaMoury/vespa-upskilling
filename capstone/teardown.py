"""
Stop and remove the local Vespa container(s). Run this when you're done, or before
a clean re-deploy.

    Usage:  python teardown.py
"""

import subprocess


def main():
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "ancestor=vespaengine/vespa"],
        capture_output=True,
        text=True,
    )
    ids = [x for x in out.stdout.split() if x]
    if not ids:
        print("No Vespa containers found. Nothing to do.")
        return
    print(f"Removing {len(ids)} Vespa container(s): {', '.join(ids)}")
    subprocess.run(["docker", "rm", "-f", *ids])
    print("Done. (The downloaded image is kept; `docker rmi vespaengine/vespa` to remove it too.)")


if __name__ == "__main__":
    main()
