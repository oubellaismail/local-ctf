#!/usr/bin/env python3
import sys
import os

# Line-buffered stdout for socat compatibility
sys.stdout = open(sys.stdout.fileno(), 'w', buffering=1)

FLAG = os.environ['FLAG']

# Step 1: Answer the first riddle
sys.stdout.write("Question 1: What is the answer to life, the universe, and everything? ")
sys.stdout.flush()
answer1 = sys.stdin.readline().strip()

if answer1 != "42":
    print("Wrong answer. Goodbye.")
    sys.exit(1)

# Step 2: Answer the second riddle
sys.stdout.write("Question 2: What colour is the sky? ")
sys.stdout.flush()
answer2 = sys.stdin.readline().strip().lower()

if answer2 != "blue":
    print("Wrong answer. Goodbye.")
    sys.exit(1)

# All correct: return the flag
print(f"Correct! Here is your flag: {FLAG}")
sys.exit(0)
