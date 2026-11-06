import os, sys

FLAG = os.environ["FLAG"]  # injected by K8s Secret at runtime

def main():
    sys.stdout.write("InfoDays CTF — OSINT Challenge\n")
    sys.stdout.write("================================\n")
    sys.stdout.write("What year was DigitalOcean founded?\n> ")
    sys.stdout.flush()

    answer = sys.stdin.readline().strip()
    if answer == "2011":
        sys.stdout.write(f"Correct! Here is your flag: {FLAG}\n")
    else:
        sys.stdout.write("Wrong. Keep researching!\n")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
