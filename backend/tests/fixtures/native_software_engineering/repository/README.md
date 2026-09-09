# Native software-engineering fixture

This repository contains one deliberately documented bug for the offline
execution contract. `add(left, right)` subtracts `right`, even though its
name and test contract require addition. The bounded workflow must inspect the
bug, plan the exact one-line replacement, patch a job-owned copy, run the
focused test in an independent process, and read the result back.

The source fixture is immutable proof input. A run must leave this directory
unchanged and must keep its recoverable workspace and receipts when execution
fails, times out, or is cancelled.
